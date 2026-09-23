"""The server's view of its ezMM media registry.

Two jobs. First, describe retrieved media well enough that a client can pick it up
without a copy being made: each item's *host-visible* path goes into the manifest, so
a client on the same machine adopts the file where it lies. Inside Docker the server's
own path (`/data/media/...`) means nothing to anyone outside the container, which is
why SCRAPEMM_MEDIA_HOST_DIR exists -- the compose file sets it to whatever the volume
is mounted from on the host.

Second, make the registry identifiable. A random fingerprint is written into its root
once, and a client compares it against its own registry's to find out whether the two
are literally the same one.
"""

import logging
import shutil
import time
import os
import uuid
from pathlib import Path
from typing import Optional

from ezmm import Item, MultimodalSequence
from ezmm.common.registry import item_registry

from scrapemm.common.paths import APP_NAME
from scrapemm.common.wire import ItemDescriptor, RegistryInfo

logger = logging.getLogger(APP_NAME)

FINGERPRINT_FILENAME = ".scrapemm-registry"

# Whether the manifest advertises file paths at all. Turning this off forces every
# client to download the bytes, which is what a deployment wants when its clients are
# elsewhere and the paths would only be noise (or an unwanted disclosure of layout).
EXPOSE_PATHS = os.getenv("SCRAPEMM_MEDIA_EXPOSE_PATHS", "1").lower() not in ("0", "false", "no")

# Where the registry lives as seen from *outside* the container
HOST_DIR = os.getenv("SCRAPEMM_MEDIA_HOST_DIR")

_fingerprint: Optional[str] = None


def registry_root() -> Path:
    return Path(item_registry.path)


def fingerprint() -> str:
    """The id of this registry, created on first use and kept in the registry root."""
    global _fingerprint
    if _fingerprint is not None:
        return _fingerprint

    path = registry_root() / FINGERPRINT_FILENAME
    try:
        _fingerprint = path.read_text(encoding="utf-8").strip()
        if _fingerprint:
            return _fingerprint
    except OSError:
        pass

    _fingerprint = uuid.uuid4().hex
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_fingerprint, encoding="utf-8")
    except OSError:
        # Without the file, clients simply fall back to downloading. Not worth failing
        # a retrieval over.
        logger.warning(f"Could not write the registry fingerprint to {path}. Clients "
                       f"on this machine will download media instead of adopting it.")
    return _fingerprint


def host_path(item_path: Path) -> Optional[str]:
    """Translates a path inside the registry into the one a client sees on the host."""
    if not EXPOSE_PATHS:
        return None
    if not HOST_DIR:
        return str(item_path)  # Not containerised: our own paths are the host's
    try:
        relative = Path(item_path).relative_to(registry_root())
    except ValueError:
        return None  # Outside the registry; a client could not find it anyway
    return str(Path(HOST_DIR) / relative)


def info() -> RegistryInfo:
    """What the client needs to decide how media should reach it."""
    if not EXPOSE_PATHS:
        return RegistryInfo(root=None, fingerprint=None, writable_by_client=False)
    root = HOST_DIR or str(registry_root())
    return RegistryInfo(root=root, fingerprint=fingerprint(), writable_by_client=True)


def describe(item: Item) -> ItemDescriptor:
    """Renders one media item for the manifest."""
    path = Path(item.file_path)
    try:
        size = path.stat().st_size
    except OSError:
        logger.debug(f"Could not stat {path} for the media manifest.", exc_info=True)
        size = None

    # Deliberately no checksum: hashing every retrieved video would cost more than it
    # saves, and clients identify a downloaded file by this registry's fingerprint plus
    # the item id, which is unique without reading the file at all.
    return ItemDescriptor(
        ref=item.reference,
        kind=item.kind,
        id=item.id,
        path=host_path(path),
        media_url=f"/v1/media/{item.kind}/{item.id}",
        source_url=getattr(item, "source_url", None),
        size=size,
    )


def describe_sequence(sequence: MultimodalSequence) -> list[ItemDescriptor]:
    """Renders every item of a retrieved page for the manifest."""
    return [describe(item) for item in sequence.unique_items()]


def resolve_item(kind: str, identifier: int) -> Optional[Item]:
    """Looks up an item so the media endpoint can serve its bytes."""
    try:
        return item_registry.get(kind=kind, identifier=identifier)
    except Exception:
        logger.debug(f"No item <{kind}:{identifier}> in the registry.", exc_info=True)
        return None


# How long a measured size is served before the tree is walked again
USAGE_TTL = 60.0

_usage_cache: tuple[float, Optional[dict]] = (0.0, None)


def usage(max_age: float = USAGE_TTL) -> dict:
    """Size of the registry, for the dashboard.

    Walking the tree is O(files) and the dashboard asks on every load, so the answer is
    cached: a media directory that grew by a few megabytes in the last minute is not
    something anybody is watching that closely.
    """
    global _usage_cache
    measured_at, cached = _usage_cache
    if cached is not None and time.time() - measured_at < max_age:
        return cached

    total, count = 0, 0
    for path in registry_root().rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
                count += 1
            except OSError:
                continue

    cached = {"root": str(registry_root()), "host_root": HOST_DIR, "bytes": total,
              "files": count, "measured_at": time.time(), **_disk_usage()}
    _usage_cache = (time.time(), cached)
    return cached


def _disk_usage() -> dict:
    """How full the filesystem holding the media is.

    What matters about a media directory is not its size in isolation but how much room
    is left: 12 GB is nothing on a 4 TB volume and an emergency on a 16 GB one.
    """
    try:
        total, used, free = shutil.disk_usage(registry_root())
    except OSError:
        logger.debug("Could not read the disk usage of the media registry.", exc_info=True)
        return {"disk_total": None, "disk_used": None, "disk_free": None}
    return {"disk_total": total, "disk_used": used, "disk_free": free}
