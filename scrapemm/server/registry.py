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
import signal
import subprocess
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


# How long a measured size is served before the tree is walked again. A shared lab
# registry holds millions of files, whose walk takes long and loads the disk, so the
# dashboard's figure may well be half an hour old.
USAGE_TTL = 30 * 60.0

# A walk that takes longer than this is given up (and retried after the TTL)
WALK_TIMEOUT = 2 * 60 * 60.0

# A walk is never repeated sooner than this many times its own duration, so a registry
# of millions of files is not re-measured back to back
WALK_COST_FACTOR = 10

_usage: Optional[dict] = None  # The last complete measurement
_walk_duration = 0.0
_walk_lock = threading.Lock()
_walking = False


def usage(max_age: float = USAGE_TTL) -> dict:
    """Size of the registry, for the dashboard. Returns at once, always.

    Walking the tree is O(files), and a lab's registry holds millions of them: done in
    the request, it stalled the whole server for as long as the walk took. So this only
    ever answers from the last measurement and, when that is older than `max_age`, has
    a new one taken in the background. Until the first walk is done, the sizes are None.
    """
    refresh(max_age)
    measured = _usage or {"bytes": None, "files": None, "measured_at": None}
    # The free space is one statvfs call, so it is always current
    return {"root": str(registry_root()), "host_root": HOST_DIR, **measured, **_disk_usage()}


def refresh(min_age: float = USAGE_TTL) -> bool:
    """Starts a background walk unless one is running or the last one is younger than
    `min_age` (or than its own cost allows). Returns True only if it started one: a walk
    already under way may have begun before the files a caller wants counted."""
    global _walking
    with _walk_lock:
        if _walking:
            return False
        measured_at = (_usage or {}).get("measured_at") or 0.0
        if time.time() - measured_at < max(min_age, WALK_COST_FACTOR * _walk_duration):
            return False
        _walking = True
    threading.Thread(target=_walk, name="media-usage", daemon=True).start()
    return True


def _walk() -> None:
    global _usage, _walk_duration, _walking
    started = time.time()
    try:
        measured = _measure_with_find()
        if measured is None:
            measured = _measure_in_python()
        total, count = measured
        _usage = {"bytes": total, "files": count, "measured_at": time.time()}
        _walk_duration = time.time() - started
        logger.debug(f"Measured the media registry: {count} files in {_walk_duration:.1f}s.")
    except Exception:
        logger.debug("Measuring the media registry failed.", exc_info=True)
    finally:
        with _walk_lock:
            _walking = False


# Sums up the sizes in the subprocess, so that Python reads a single line. `%.0f`, as
# some awks print `%d` as a 32-bit integer.
_FIND_SCRIPT = r"""find "$1" -type f -printf '%s\n' 2>/dev/null | awk '{s += $1; n++} END {printf "%.0f %d\n", s, n}'"""


def _measure_with_find() -> Optional[tuple[int, int]]:
    """Total size and number of files, measured by `find` in a subprocess at the lowest
    CPU priority. A walk in Python over millions of files held the GIL for most of the
    time and so stalled the event loop, i.e. every retrieval. None where there is no
    GNU find (e.g. on Windows)."""
    if os.name != "posix" or not shutil.which("find") or not shutil.which("awk"):
        return None
    process = subprocess.Popen(
        ["nice", "-n", "19", "sh", "-c", _FIND_SCRIPT, "sh", str(registry_root())],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        stdout, _ = process.communicate(timeout=WALK_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)  # The whole pipeline, not just the shell
        process.wait()
        raise TimeoutError(f"Measuring the media registry took over {WALK_TIMEOUT:.0f} s.")
    total, count = stdout.split()
    return int(total), int(count)


def _measure_in_python() -> tuple[int, int]:
    """The fallback where there is no `find`. Holds the GIL for much of the walk."""
    total, count = 0, 0
    pending = [str(registry_root())]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, count


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
