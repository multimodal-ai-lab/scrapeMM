"""Turning a server's media manifest back into a local `MultimodalSequence`.

The point of this module is to *not* copy files. A `MultimodalSequence` is text with
`<image:3>`-style references into an ezMM registry, and when the client runs on the
same machine as the server -- the common case for a lab that deploys scrapeMM next to
its pipeline -- those files are already on this filesystem. Downloading them would
produce a second copy of every image and video the server just saved.

So the manifest carries each file's host-visible path, and the client picks the
cheapest mode that works:

* **shared**   -- this process' ezMM registry *is* the server's. The references are
                  already valid; nothing happens at all.
* **link**     -- the registry is a different one, but the server's files are readable
                  from here. They are registered by path, which ezMM stores verbatim,
                  so still not a byte is copied.
* **download** -- genuinely remote. The bytes come over the API.

A registry is recognised by a fingerprint file the server writes into its root, so
"same machine" is established by looking at the actual filesystem rather than by
comparing hostnames.
"""

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

import aiohttp
from ezmm import Item, MultimodalSequence
from ezmm.common.items import KIND2ITEM
from ezmm.common.registry import item_registry

from scrapemm.common import APP_NAME
from scrapemm.common.wire import ContentPayload, ItemDescriptor, RegistryInfo

logger = logging.getLogger(APP_NAME)

# The file the server drops into its registry root to make it identifiable
FINGERPRINT_FILENAME = ".scrapemm-registry"

REF_REGEX = re.compile(r"<(image|video|audio):(\d+)>")

# Remembers what was decided for a given server registry, so the filesystem is not
# re-examined for every single retrieved URL.
_mode_cache: dict[tuple[Optional[str], Optional[str]], str] = {}


def _local_fingerprint() -> Optional[str]:
    """The fingerprint of *this* process' ezMM registry, if it has one."""
    return _read_fingerprint(item_registry.path)


def _read_fingerprint(root: Path | str) -> Optional[str]:
    try:
        return (Path(root) / FINGERPRINT_FILENAME).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def resolve_mode(registry: RegistryInfo, preference: str = "auto") -> str:
    """Decides how the media of this server should reach us."""
    if preference != "auto":
        return preference

    if not registry.root or not registry.fingerprint:
        return "download"  # The server does not expose its files

    key = (registry.root, registry.fingerprint)
    if (cached := _mode_cache.get(key)) is not None:
        return cached

    mode = "download"
    if _local_fingerprint() == registry.fingerprint:
        # Same registry: the ids in the manifest are the ids we already know
        mode = "shared"
    elif registry.writable_by_client and _read_fingerprint(registry.root) == registry.fingerprint:
        # Different registry, same files: adopt them where they lie
        mode = "link"

    _mode_cache[key] = mode
    logger.debug(f"Media transfer mode for registry {registry.root}: {mode}")
    return mode


async def resolve_content(content: ContentPayload,
                          registry: RegistryInfo,
                          session: aiohttp.ClientSession,
                          base_url: str,
                          headers: dict,
                          preference: str = "auto") -> Optional[MultimodalSequence]:
    """Rebuilds the multimodal sequence described by `content`, materialising its
    media as cheaply as the deployment allows. Returns None if the server produced
    no multimodal content."""
    if content.multimodal is None:
        return None

    mode = resolve_mode(registry, preference)
    if mode == "shared":
        return MultimodalSequence(content.multimodal)

    items = await _materialize(content.items, mode, registry, session, base_url, headers)
    return MultimodalSequence(_remap(content.multimodal, items))


async def _materialize(descriptors: list[ItemDescriptor], mode: str,
                       registry: RegistryInfo, session: aiohttp.ClientSession,
                       base_url: str, headers: dict) -> dict[str, Optional[Item]]:
    """Produces a local item for every descriptor, keyed by the server's reference."""
    results = await asyncio.gather(
        *(_materialize_one(d, mode, registry, session, base_url, headers)
          for d in descriptors),
        return_exceptions=True,
    )
    items: dict[str, Optional[Item]] = {}
    for descriptor, result in zip(descriptors, results):
        if isinstance(result, BaseException):
            logger.warning(f"Could not obtain media {descriptor.ref}: {result}")
            items[descriptor.ref] = None
        else:
            items[descriptor.ref] = result
    return items


async def _materialize_one(descriptor: ItemDescriptor, mode: str, registry: RegistryInfo,
                           session: aiohttp.ClientSession, base_url: str,
                           headers: dict) -> Optional[Item]:
    item_cls = KIND2ITEM.get(descriptor.kind)
    if item_cls is None:
        logger.warning(f"Unknown media kind '{descriptor.kind}'; skipping {descriptor.ref}.")
        return None

    if mode == "link" and descriptor.path:
        path = Path(descriptor.path)
        if path.exists():
            # ezMM stores the path verbatim, so this registers the server's file
            # without copying it. Registering is pure SQLite work, hence the thread.
            return await asyncio.to_thread(
                item_cls, file_path=path, source_url=descriptor.source_url)
        logger.debug(f"{path} is not readable after all; downloading {descriptor.ref}.")

    return await _download(descriptor, item_cls, registry, session, base_url, headers)


async def _download(descriptor: ItemDescriptor, item_cls, registry: RegistryInfo,
                    session: aiohttp.ClientSession, base_url: str,
                    headers: dict) -> Optional[Item]:
    if not descriptor.media_url:
        return None

    url = descriptor.media_url
    if not url.startswith(("http://", "https://")):
        url = f"{base_url}/{url.lstrip('/')}"

    # The file is named after the server it came from plus that server's item id, which
    # identifies it uniquely without anybody having to hash a 200 MB video -- and keeps
    # two servers' item 7 from landing on top of each other in one client registry.
    origin = (registry.fingerprint or "server")[:8]
    suffix = Path(descriptor.path or "").suffix
    target = item_registry.path / descriptor.kind
    target.mkdir(parents=True, exist_ok=True)
    destination = target / f"{origin}_{descriptor.id}{suffix}"

    if not destination.exists():
        async with session.get(url, headers=headers) as response:
            response.raise_for_status()
            # Written aside first, so an interrupted download cannot leave a truncated
            # file that the next run would happily adopt.
            partial = destination.with_suffix(destination.suffix + ".part")
            with open(partial, "wb") as f:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    f.write(chunk)
            partial.replace(destination)

    return await asyncio.to_thread(
        item_cls, file_path=destination, source_url=descriptor.source_url)


def _remap(text: str, items: dict[str, Optional[Item]]) -> str:
    """Rewrites the server's references to the ids this registry assigned. Done in one
    pass so that a new id colliding with an old one cannot be rewritten twice.

    A reference whose media could not be obtained is dropped rather than left dangling:
    `MultimodalSequence` would refuse to resolve it and lose the text along with it.
    """
    def substitute(match: re.Match) -> str:
        item = items.get(match.group(0), "missing")
        if item == "missing":
            return match.group(0)  # Not ours to touch
        return item.reference if item is not None else ""

    return REF_REGEX.sub(substitute, text)
