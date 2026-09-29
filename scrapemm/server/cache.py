"""scrapeMM's cache, in two tiers.

* **Recent** (in memory): successful results, for a configurable lifetime. Configurable at
  runtime (Settings in the web UI, `cache_*` in config.yaml): whether it is used at all,
  how long an entry lives, and how many entries -- or how much memory -- it may hold at
  most. The oldest entries go first once a limit is reached.
* **Permanent** (on disk, no expiry): what archives serve, which never changes -- results
  for archive captures (an Archive.today snapshot, a Wayback capture with its timestamp,
  a Perma.cc record, a Ghostarchive archive), archived media (as references to the
  media-registry items they became, never their bytes), and Archive.today's pages
  and snapshot metadata. A capture retrieved once is served again without any network,
  and Archive.today's gated pages stay retrievable long after the session that got them
  expired. Bounded by its own size (`cache_immutable_max_mb`, on disk); the least
  recently used entries go first. Switching the recent tier off leaves this one on.
  See `ImmutableCache`.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from scrapemm.common.scraping_response import ScrapingResponse, OutputFormat

DEFAULT_CACHE_TTL = 24 * 60 * 60  # 24 hours
# Every result is cached, whether or not its request reads from the cache, and a page's
# HTML and Markdown come to about half a MB: without a cap, a day of scraping would pile
# up gigabytes of them in the server's memory
DEFAULT_MAX_ENTRIES = 1000
DEFAULT_MAX_MB = 512  # The text the entries hold, see `_size()`; media stay on disk
MAX_ENTRIES = DEFAULT_MAX_ENTRIES  # Kept for callers of the former constant

# A cache entry is identified by all the parameters that influence the retrieved content:
# the URL, the requested output format, the methods used, and the video size limit
CacheKey = tuple[str, str, tuple[str, ...], Optional[int]]


def cache_key(url: str, output_format: OutputFormat, methods: list[str],
              max_video_size: Optional[int] = None) -> CacheKey:
    """Constructs the cache key identifying a particular retrieval request."""
    return url, output_format, tuple(methods), max_video_size


def _size(response: ScrapingResponse) -> int:
    """Roughly the memory an entry takes: the characters of its text formats. Media are
    files in the registry and cost the cache only a reference each."""
    content = response.content
    if content is None:
        return 0
    multimodal = str(content.multimodal) if content.multimodal is not None else ""
    return len(content.html or "") + len(content.markdown or "") + len(multimodal)


class ScrapeCache:
    """Keeps successful scraping results in memory for `ttl` seconds. Not persisted,
    i.e., the cache is empty again after the process terminated."""

    def __init__(self, ttl: float = DEFAULT_CACHE_TTL):
        self.ttl = ttl
        self.enabled = True
        self.max_entries = DEFAULT_MAX_ENTRIES
        self.max_bytes = DEFAULT_MAX_MB * 1024 * 1024
        self._entries: dict[CacheKey, tuple[float, ScrapingResponse, int]] = {}
        self._bytes = 0
        self._prune_at = 128  # Number of entries at which to prune expired ones

    @property
    def active(self) -> bool:
        return self.enabled and self.ttl > 0

    def get(self, key: CacheKey) -> Optional[ScrapingResponse]:
        """Returns the cached response for `key` if there is a non-expired one, or the
        permanent tier's one for an archive capture."""
        response = self._get_recent(key)
        if response is None:
            response = immutable.result(key)
            if response is not None:
                self._put_recent(key, response)
        return response

    def _get_recent(self, key: CacheKey) -> Optional[ScrapingResponse]:
        if not self.active:
            return None
        entry = self._entries.get(key)
        if entry is None:
            return None
        timestamp, response, _ = entry
        if time.time() - timestamp > self.ttl:
            self._drop(key)
            return None
        return response

    def put(self, key: CacheKey, response: ScrapingResponse) -> None:
        """Caches the given response: in the recent tier (unless it is off), and in the
        permanent tier if it is a complete result for an archive capture."""
        immutable.keep_result(key, response)
        self._put_recent(key, response)

    def _put_recent(self, key: CacheKey, response: ScrapingResponse) -> None:
        if not self.active:
            return
        if len(self._entries) >= self._prune_at:
            self._prune()
        self._drop(key)  # Re-inserted at the end, i.e. as the newest
        size = _size(response)
        if size > self.max_bytes:
            return  # A single page larger than the whole cache
        self._entries[key] = (time.time(), response, size)
        self._bytes += size
        self._enforce_limits()

    def configure(self, enabled: Optional[bool] = None, ttl: Optional[float] = None,
                  max_entries: Optional[int] = None, max_mb: Optional[float] = None) -> None:
        """Applies new settings at once: entries beyond a lowered limit are dropped, and
        switching the cache off empties it."""
        if enabled is not None:
            self.enabled = bool(enabled)
        if ttl is not None:
            self.ttl = float(ttl)
        if max_entries is not None:
            self.max_entries = max(1, int(max_entries))
        if max_mb is not None:
            self.max_bytes = max(1, int(float(max_mb) * 1024 * 1024))
        if not self.active:
            self.clear()
        else:
            self._prune()
            self._enforce_limits()

    def stats(self) -> dict:
        return {"entries": len(self._entries), "size_mb": round(self._bytes / 2 ** 20, 2),
                "enabled": self.enabled, "ttl": self.ttl, "max_entries": self.max_entries,
                "max_mb": round(self.max_bytes / 2 ** 20, 1),
                "immutable": immutable.stats()}

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0

    def _drop(self, key: CacheKey) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._bytes -= entry[2]

    def _enforce_limits(self) -> None:
        while self._entries and (len(self._entries) > self.max_entries
                                 or self._bytes > self.max_bytes):
            self._drop(next(iter(self._entries)))  # The oldest

    def _prune(self) -> None:
        """Drops all expired entries."""
        deadline = time.time() - self.ttl
        for key in [k for k, v in self._entries.items() if v[0] < deadline]:
            self._drop(key)
        self._prune_at = max(128, 2 * len(self._entries))

    def __len__(self) -> int:
        return len(self._entries)


cache = ScrapeCache()


def set_cache_ttl(seconds: float) -> None:
    """Sets how long (in seconds) scraping results are re-used from the cache.
    Use 0 (or any negative value) to disable caching."""
    cache.ttl = seconds


def clear_cache() -> None:
    """Removes all entries from the scraping cache's recent tier."""
    cache.clear()


# --- The permanent tier -------------------------------------------------------------

logger = logging.getLogger("scrapeMM")

DEFAULT_IMMUTABLE_MAX_MB = 10 * 1024

# Bumped whenever extraction improves in a way that kept results should not hide.
# Results are also keyed by scrapeMM's minor version: a release re-retrieves them once.
RESULT_FORMAT = 1

ARCHIVE_TODAY_HOSTS = ("archive.today", "archive.is", "archive.ph", "archive.vn",
                       "archive.li", "archive.fo", "archive.md")

# Kinds of entries. Results and media come from any archive; pages and snapshots are
# Archive.today's replay content and capture metadata (see `integrations.archive_today`).
RESULT, MEDIUM, PAGE, SNAPSHOT = "result", "medium", "page", "snapshot"
KINDS = (RESULT, MEDIUM, PAGE, SNAPSHOT)

_ARCHIVE_TODAY_CAPTURE = re.compile(
    r"^/(?:wip/)?([A-Za-z0-9]{4,6}|\d{4}\.\d\d\.\d\d-\d{6}/.+|\d{14}/.+?)/?$")
_WAYBACK_CAPTURE = re.compile(r"^/web/\d{14}(?:[a-z]{2}_)?/.+")
_PERMA_RECORD = re.compile(r"^/([0-9A-Z]{4}-[0-9A-Z]{4})/?$")
_GHOSTARCHIVE = re.compile(r"^/v?archive/[A-Za-z0-9]+/?$")
_ARCHIVE_TODAY_MEDIUM = re.compile(r"^/[A-Za-z0-9]+/[0-9a-f]{40}(?:/[^/]+)?\.[A-Za-z0-9]{2,5}$")
# A ReplayWeb.page replay (Perma.cc, Ghostarchive) names its media under a per-load
# session id; what follows it (a timestamp, if any, and the archived URL) is what counts
_REPLAY_MEDIUM = re.compile(r"/w/id-[0-9a-f]+/(.+)$")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def _with_query(parts) -> str:
    return parts.path + (f"?{parts.query}" if parts.query else "")


def capture_key(url: str) -> Optional[str]:
    """The canonical form of an archive capture's URL, or None if the URL is not one.
    A capture is what never changes: an Archive.today snapshot, a Wayback capture with
    an explicit timestamp, a Perma.cc record, a Ghostarchive archive."""
    parts = urlsplit(str(url))
    host = _host(url)
    if host in ARCHIVE_TODAY_HOSTS and (m := _ARCHIVE_TODAY_CAPTURE.match(parts.path)):
        return f"archive.ph/{m.group(1)}"
    if host == "web.archive.org" and _WAYBACK_CAPTURE.match(parts.path):
        return "web.archive.org" + _with_query(parts)
    if host == "perma.cc" and (m := _PERMA_RECORD.match(parts.path)):
        return f"perma.cc/{m.group(1)}" + ("?type=image" if "type=image" in parts.query else "")
    if host == "ghostarchive.org" and _GHOSTARCHIVE.match(parts.path):
        return "ghostarchive.org" + parts.path.rstrip("/")
    return None


def media_key(url: str, page_url: Optional[str] = None) -> Optional[str]:
    """The key of an archived medium, or None if `url` is not one: Archive.today's
    content-addressed media, Wayback media with a timestamp, and media in the replay of
    the capture `page_url` (e.g. a Perma.cc record)."""
    if not url or not str(url).startswith(("http://", "https://")):
        return None
    parts = urlsplit(str(url))
    host = _host(url)
    if host.endswith(ARCHIVE_TODAY_HOSTS) and _ARCHIVE_TODAY_MEDIUM.match(parts.path):
        return f"archive.ph{parts.path}"
    if host == "web.archive.org" and _WAYBACK_CAPTURE.match(parts.path):
        return "web.archive.org" + _with_query(parts)
    if page_url and (capture := capture_key(page_url)) and (
            m := _REPLAY_MEDIUM.search(_with_query(parts))):
        return f"{capture}#{m.group(1)}"
    return None


def _result_version() -> str:
    from scrapemm import __version__
    return ".".join(__version__.split(".")[:2]) + f"/{RESULT_FORMAT}"


def _archived_url(capture: str) -> Optional[str]:
    """The URL a capture archived, where the capture's own URL says it."""
    if m := re.search(r"/(?:\d{14}(?:[a-z]{2}_)?|\d{4}\.\d\d\.\d\d-\d{6})/(.+)$", capture):
        return m.group(1)
    return None


# Archived media that could not be downloaded lately, by media key: a result whose page
# names one of them lacks it, and is not kept for good (the archive may only have been
# slow -- archive.ph delays requests after a burst)
_failed_media: dict[str, float] = {}
FAILED_MEDIA_MEMORY = 3600.0
_SRC = re.compile(r"""\b(?:src|poster|data-src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def note_failed_medium(url: str, page_url: Optional[str] = None) -> None:
    if (key := media_key(url, page_url)) is not None:
        now = time.monotonic()
        _failed_media[key] = now
        if len(_failed_media) > 10_000:
            for k in [k for k, t in _failed_media.items() if now - t > FAILED_MEDIA_MEMORY]:
                _failed_media.pop(k, None)


def _lacks_media(url: str, html: Optional[str]) -> bool:
    """Whether a medium the page names failed to download lately."""
    if not html or not _failed_media:
        return False
    from urllib.parse import urljoin
    now = time.monotonic()
    for src in _SRC.findall(html):
        key = media_key(urljoin(url, src), url)
        if key is not None and now - _failed_media.get(key, -FAILED_MEDIA_MEMORY) < FAILED_MEDIA_MEMORY:
            return True
    return False


def complete_capture(url: str, response: ScrapingResponse) -> bool:
    """Whether a result may be kept for good: a successful one for an archive capture,
    not a stand-in (e.g. a capture's screenshot in place of its page), without a medium
    that failed to download, and not missing a video -- one its page shows, or the one
    a video URL archived."""
    content = response.content
    capture = capture_key(url)
    if capture is None or not response.success or content is None:
        return False
    if getattr(content, "stand_in", False):
        return False
    if _lacks_media(url, content.html):
        return False
    multimodal = content.multimodal
    if multimodal is not None and multimodal.videos:
        return True
    if content.html and "<video" in content.html.lower():
        return False
    from .chain import is_video_url
    archived = _archived_url(capture)
    return not (archived and is_video_url(archived))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ImmutableCache:
    """The permanent tier: files on disk, one per entry, a directory per kind. The least
    recently used go first once `max_bytes` is exceeded; reading an entry refreshes its
    file time."""

    def __init__(self, directory: Optional[Path] = None):
        if directory is None:
            from .paths import CONFIG_DIR
            directory = CONFIG_DIR / "cache"
        self.directory = directory
        self.max_bytes = DEFAULT_IMMUTABLE_MAX_MB * 1024 * 1024
        self._lock = threading.Lock()
        self._counts: Optional[dict[str, int]] = None  # By kind, once counted
        self._bytes = 0
        self._counting = False
        self._evicting = False

    # -- Plain entries ---------------------------------------------------------------

    def _file(self, kind: str, key: str) -> Path:
        return self.directory / kind / hashlib.sha1(key.encode("utf-8")).hexdigest()

    def get(self, kind: str, key: str) -> Optional[bytes]:
        file = self._file(kind, key)
        try:
            data = file.read_bytes()
        except OSError:
            return None
        with suppress(OSError):
            os.utime(file)  # Recently used
        return data

    def contains(self, kind: str, key: str) -> bool:
        return self._file(kind, key).exists()

    def put(self, kind: str, key: str, data: bytes) -> None:
        if not data:
            return
        file = self._file(kind, key)
        try:
            previous = file.stat().st_size
        except OSError:
            previous = None
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            partial = file.with_name(f"{file.name}.{threading.get_ident()}.part")
            partial.write_bytes(data)
            partial.replace(file)
        except OSError:
            logger.debug(f"Could not keep the {kind} {key[:120]} in the cache.", exc_info=True)
            return
        self._account(kind, len(data) - (previous or 0), 1 if previous is None else 0)

    def adopt(self, kind: str, key: str, source: Path) -> None:
        """Moves an existing file in as the entry (from a former cache's directory)."""
        file = self._file(kind, key)
        file.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, file)
        self._account(kind, file.stat().st_size, 1)

    def _account(self, kind: str, size_change: int, count_change: int) -> None:
        with self._lock:
            if self._counts is None:
                return
            self._bytes += size_change
            self._counts[kind] = self._counts.get(kind, 0) + count_change
            over = self._bytes > self.max_bytes
        if over:
            threading.Thread(target=self._evict, daemon=True, name="cache-evict").start()

    def count(self, kind: str) -> Optional[int]:
        counts = self._ensure_counted()
        return None if counts is None else counts.get(kind, 0)

    # -- Results ---------------------------------------------------------------------

    @staticmethod
    def _result_key(key) -> Optional[str]:
        url, output_format, _methods, max_video_size = key
        if (capture := capture_key(url)) is None:
            return None
        return f"{capture}|{output_format}|{max_video_size}|{_result_version()}"

    def result(self, key) -> Optional[ScrapingResponse]:
        """The kept result for the cache key of an archive capture, if there is one and
        all its media are still in the registry."""
        if (entry_key := self._result_key(key)) is None:
            return None
        if (data := self.get(RESULT, entry_key)) is None:
            logger.info(f"📦 Permanent cache miss for {key[0]}.")
            return None
        try:
            entry = json.loads(data)
            multimodal = None
            if entry.get("multimodal") is not None:
                from ezmm import MultimodalSequence
                # Resolves the media references from the registry; raises for one gone
                multimodal = MultimodalSequence(entry["multimodal"])
        except (ValueError, KeyError, TypeError) as e:
            logger.info(f"📦 The permanent cache's result for {key[0]} is unusable ({e}); "
                        f"retrieving it afresh.")
            return None
        from scrapemm.common.scraping_response import ScrapedContent
        content = ScrapedContent(html=entry.get("html"), markdown=entry.get("markdown"),
                                 multimodal=multimodal)
        logger.info(f"📦 Permanent cache hit for {key[0]}.")
        return ScrapingResponse(url=key[0], content=content, method=entry.get("method"),
                                output_format=key[1], retrieval_time=0.0, from_cache=True)

    def keep_result(self, key, response: ScrapingResponse) -> None:
        if (entry_key := self._result_key(key)) is None:
            return
        if not complete_capture(key[0], response):
            logger.debug(f"Not keeping {key[0]} for good: not a complete capture.")
            return
        content = response.content
        entry = {"url": key[0], "method": response.method, "html": content.html,
                 "markdown": content.markdown, "stored_at": time.time(),
                 "multimodal": None if content.multimodal is None else str(content.multimodal)}
        self.put(RESULT, entry_key, json.dumps(entry).encode("utf-8"))

    # -- Media -----------------------------------------------------------------------

    # An archived medium is kept as a reference to the ezMM registry item it became --
    # the registry holds the file, which the server never deletes -- so nothing is
    # stored twice. A hit re-uses that item: no copy, no network. The file's size and
    # hash are recorded on first use (the medium may still be normalized right after it
    # was kept, see `engine.postprocess_media()`); a file gone or changed since is a
    # miss. An image that became no item (too small, or not decodable) is remembered as
    # such, so it is not downloaded again only to be dropped again.

    async def item(self, url: str, page_url: Optional[str] = None, variant: str = "",
                   ignore_small: bool = True, max_bytes: Optional[int] = None) -> tuple[bool, object]:
        """(True, item) for a kept archived medium, (True, None) for one known to become
        no item, (False, None) otherwise. `variant` distinguishes differently produced
        items of one URL (e.g. another image size limit)."""
        if (key := media_key(url, page_url)) is None:
            return False, None
        return await asyncio.to_thread(self._item, key + variant, url, ignore_small, max_bytes)

    def _item(self, key: str, url: str, ignore_small: bool,
              max_bytes: Optional[int]) -> tuple[bool, object]:
        data = self.get(MEDIUM, key)
        if data is None:
            return False, None
        try:
            entry = json.loads(data)
        except ValueError:
            return False, None
        if entry.get("none"):
            return (True, None) if ignore_small else (False, None)
        from ezmm.common.registry import item_registry
        try:
            item = item_registry.get(entry["ref"])
            path = Path(item.file_path) if item is not None else None
            if path is None or str(path) != entry["path"] or not path.is_file():
                raise LookupError("the file is gone")
            size = path.stat().st_size
            if entry.get("size") is None:  # First use: from now on, the file must not change
                entry.update(size=size, sha256=_sha256(path))
                self.put(MEDIUM, key, json.dumps(entry).encode("utf-8"))
            elif size != entry["size"] or _sha256(path) != entry["sha256"]:
                raise LookupError("the file changed")
        except (LookupError, KeyError, ValueError, OSError) as e:
            logger.info(f"📦 The kept medium {url[:120]} is unusable ({e}); downloading it again.")
            return False, None
        if max_bytes is not None and size > max_bytes:
            return True, None  # Too large for this request, as a download would find too
        logger.debug(f"📦 Medium {url[:120]} is {entry['ref']} from the permanent cache.")
        return True, item

    async def keep_item(self, url: str, item, page_url: Optional[str] = None,
                        variant: str = "") -> None:
        """Remembers the registry item an archived medium became, or that it became none
        (`item` None, e.g. an image too small to keep)."""
        if (key := media_key(url, page_url)) is None:
            return
        if item is None:
            entry = {"none": True}
        else:
            entry = {"ref": item.reference, "path": str(Path(item.file_path)),
                     "size": None, "sha256": None}
        await asyncio.to_thread(self.put, MEDIUM, key + variant,
                                json.dumps(entry).encode("utf-8"))

    # -- Housekeeping ----------------------------------------------------------------

    def configure(self, max_mb: Optional[float] = None) -> None:
        if max_mb is not None:
            self.max_bytes = max(1, int(float(max_mb) * 1024 * 1024))
            self._account(RESULT, 0, 0)  # Makes room at once if the cap went down

    def _ensure_counted(self) -> Optional[dict[str, int]]:
        """The entries by kind, counted once in the background (there may be hundreds
        of thousands); None until then."""
        with self._lock:
            if self._counts is not None or self._counting:
                return self._counts
            self._counting = True
        threading.Thread(target=self._count, daemon=True, name="cache-count").start()
        return None

    def _files(self):
        for kind in KINDS:
            with suppress(OSError):
                for entry in os.scandir(self.directory / kind):
                    if entry.is_file() and not entry.name.endswith(".part"):
                        yield kind, entry

    def _count(self) -> None:
        counts, total = {}, 0
        for kind, entry in self._files():
            with suppress(OSError):
                total += entry.stat().st_size
                counts[kind] = counts.get(kind, 0) + 1
        with self._lock:
            self._counts, self._bytes, self._counting = counts, total, False
        self._account(RESULT, 0, 0)

    def _evict(self) -> None:
        with self._lock:
            if self._evicting:
                return
            self._evicting = True
        try:
            files = []
            for kind, entry in self._files():
                with suppress(OSError):
                    stat = entry.stat()
                    files.append((stat.st_mtime, stat.st_size, kind, entry.path))
            files.sort()
            total = sum(size for _, size, _, _ in files)
            target = int(self.max_bytes * 0.9)  # Room, so this does not run on every write
            removed: dict[str, int] = {}
            for _, size, kind, path in files:
                if total <= target:
                    break
                with suppress(OSError):
                    os.unlink(path)
                    total -= size
                    removed[kind] = removed.get(kind, 0) + 1
            with self._lock:
                self._bytes = total
                if self._counts is not None:
                    for kind, n in removed.items():
                        self._counts[kind] = max(0, self._counts.get(kind, 0) - n)
            if removed:
                logger.info(f"The permanent cache made room: removed its least recently "
                            f"used entries ({removed}).")
        finally:
            self._evicting = False

    def stats(self) -> dict:
        counts = self._ensure_counted()
        return {"entries": None if counts is None else {k: counts.get(k, 0) for k in KINDS},
                "size_mb": None if counts is None else round(self._bytes / 2 ** 20, 1),
                "max_mb": round(self.max_bytes / 2 ** 20, 1)}

    def clear(self, kinds: tuple[str, ...] = KINDS) -> int:
        """Removes the entries of the given kinds, file by file. Returns how many."""
        removed = 0
        for kind, entry in list(self._files()):
            if kind in kinds:
                with suppress(OSError):
                    os.unlink(entry.path)
                    removed += 1
        with self._lock:
            self._counts, self._counting = None, False
        return removed


immutable = ImmutableCache()
