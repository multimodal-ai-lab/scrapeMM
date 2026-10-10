"""The server's view of its ezMM media registry.

Two jobs. First, describe retrieved media well enough that a client can pick it up
without a copy being made: each item's *host-visible* path goes into the manifest, so
a client on the same machine adopts the file where it lies. Inside Docker the server's
own path (`/data/media/...`) means nothing to anyone outside the container, which is
why the compose file hands SCRAPEMM_MEDIA_DIR -- the host directory the volume is
mounted from -- to the server as well.

Second, make the registry identifiable. A random fingerprint is written into its root
once, and a client compares it against its own registry's to find out whether the two
are literally the same one.
"""

import logging
import shutil
import threading
import time
import os
import re
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

# Where the registry lives as seen from *outside* the container. Only an absolute path
# helps a client; a relative one is relative to the compose file, which we cannot see.
HOST_DIR = os.getenv("SCRAPEMM_MEDIA_DIR")
if HOST_DIR and not re.match(r"^([A-Za-z]:)?[\\/]", HOST_DIR):
    logger.info(f"SCRAPEMM_MEDIA_DIR={HOST_DIR!r} is not absolute, so clients on this "
                f"host cannot adopt media files and will download them instead.")
    HOST_DIR = None

_fingerprint: Optional[str] = None


def open_registry() -> None:
    """Connects to the media registry, migrating it first if an older ezMM made it."""
    if item_registry.conn is None:
        started = time.time()
        item_registry.connect()
        if (took := time.time() - started) > 5:
            logger.info(f"Opened the media registry in {took:.0f} s (migrated, presumably).")


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


# How long a measured size is served before it is measured again. The figure comes from
# the registry's database (ezMM keeps each item's size), two queries of a quarter second
# each on a registry of millions, so the dashboard can follow it closely.
USAGE_TTL = 15.0

# A measurement is never repeated sooner than this many times its own duration
MEASURE_COST_FACTOR = 10

_usage: Optional[dict] = None  # The last complete measurement
_measure_duration = 0.0
_measure_lock = threading.Lock()
_measuring = False


def usage(max_age: float = USAGE_TTL) -> dict:
    """Size of the registry, for the dashboard: its items (duplicates and items whose file
    is missing left out, as ezMM counts them) and their total size. Returns at once,
    always, from the last measurement, and has a new one taken in the background when
    that is older than `max_age`. Until the first one is done, the sizes are None.

    Not the files under the root: walking them took hours on a registry of millions,
    so the figure lagged far behind (still counting 1.9 million duplicates' files the
    morning after ezMM had removed them), and it counted files no item refers to."""
    refresh(max_age)
    measured = _usage or {"bytes": None, "files": None, "measured_at": None}
    # The free space is one statvfs call, so it is always current
    return {"root": str(registry_root()), "host_root": HOST_DIR, **measured, **_disk_usage()}


def refresh(min_age: float = USAGE_TTL) -> bool:
    """Starts a background measurement unless one is running or the last one is younger
    than `min_age` (or than its own cost allows). Returns True only if it started one."""
    global _measuring
    with _measure_lock:
        if _measuring:
            return False
        measured_at = (_usage or {}).get("measured_at") or 0.0
        if time.time() - measured_at < max(min_age, MEASURE_COST_FACTOR * _measure_duration):
            return False
        _measuring = True
    threading.Thread(target=_measure, name="media-usage", daemon=True).start()
    return True


def _measure() -> None:
    """In a thread of its own: the queries scan the items table, and SQLite lets go of
    the GIL meanwhile."""
    global _usage, _measure_duration, _measuring
    started = time.time()
    try:
        count = item_registry.count_items(include_missing=False)
        total = item_registry.total_size()
        _usage = {"bytes": total, "files": count, "measured_at": time.time()}
        _measure_duration = time.time() - started
    except Exception:
        logger.debug("Measuring the media registry failed.", exc_info=True)
    finally:
        with _measure_lock:
            _measuring = False


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
