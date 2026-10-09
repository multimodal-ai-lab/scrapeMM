"""Media as the headed browser has it.

The browser downloads every medium it renders anyway. `BrowserMedia` remembers those
responses per page and hands their bytes out again instead of downloading the medium a
second time. Whatever the browser did not load in full -- videos, which it streams in byte
ranges only, lazy images below the fold, the larger srcset variant -- is fetched through
the browser, never around it: from inside the frame where a replay archive's service
worker has to answer, otherwise through the browser's own network stack.

Measured on real retrievals: the bytes of a rendered image read back from the browser are
identical to a fresh download, also for service-worker replays (Perma.cc, Ghostarchive),
and take tens of milliseconds where a second download took seconds.
"""

import asyncio
import base64
import logging
import re
from collections import Counter
from contextlib import suppress
from typing import Optional, Union
from urllib.parse import urlencode, urlparse

import aiohttp
from ezmm import Image, Video
from playwright.async_api import Page, Frame, Response
from yarl import URL

from scrapemm.server.download.common import HEADERS
from scrapemm.server import budget
from scrapemm.server.download.requests import MEDIA_TIMEOUT
from scrapemm.server.download.util import stream

from scrapemm.server.download.images import decode_image
from scrapemm.server.download.videos import video_from_binary, download_hls_video, is_hls

logger = logging.getLogger("scrapeMM")

# Largest media transferred out of the browser
MAX_IMAGE_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_VIDEO_BYTES = 250 * 1024 * 1024  # 250 MB

# Attributes the page-side scripts leave on media elements for `resolve_media()`
BLOB_ATTR = "data-scrapemm-blob"  # Id of a Blob already fetched inside the frame
CURRENT_ATTR = "data-scrapemm-current"  # The URL the browser actually rendered
BLOB_SCHEME = "scrapemm-blob:"  # How `resolve_media()` refers to such a Blob

# Bytes per message when reading a Blob or stream out of the browser: bounds the size of
# every single message, however large the video. Kept small because Playwright's Python
# client reassembles each message from 32 KB pieces by repeated concatenation -- on the
# event loop every retrieval shares, at a cost growing with the square of the message
# size. An 8 MB chunk (11 MB as base64) blocked the loop for a good fraction of a second;
# a whole video as one message, for minutes.
READ_CHUNK = 1024 * 1024

# Largest body taken from the browser in one message (`Response.body()`). Larger media,
# and media of unknown size other than images, are read in chunks or downloaded directly
# instead, see `READ_CHUNK`.
MAX_BODY_MESSAGE = 2 * 1024 * 1024

# How long to wait for the body of a response the browser is still receiving
LOADED_BODY_TIMEOUT = 60

# Defines `window.__scrapemmStash(url, opts)` in a frame, once. It fetches `url` inside
# the frame -- so a replay archive's service worker answers it -- and keeps the result as
# a Blob in the page, returning only its id. The bytes then leave the browser in chunks
# (`read_stashed()`), never as one giant data URI.
STASH_JS = r"""
if (!window.__scrapemmStash) {
  window.__scrapemmBlobs = {};
  let seq = 0;
  window.__scrapemmStash = async (url, opts = {}) => {
    // Gives up on a medium that is slow, and says so: `reason: 'timeout'`. The time can
    // be set for the whole frame (`window.__scrapemmStashTimeout`, milliseconds).
    const timeoutMs = opts.timeoutMs || window.__scrapemmStashTimeout || 0;
    const controller = new AbortController();
    const timer = timeoutMs ? setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      const res = await fetch(url, { credentials: 'include', signal: controller.signal });
      if (!res.ok) return { ok: false, reason: `HTTP ${res.status}` };
      const type = res.headers.get('content-type') || '';
      const t = type.toLowerCase();
      if (opts.skipStreaming && (t.includes('mpegurl') || /m3u8/i.test(url))) {
        return { ok: false, reason: 'streaming' };
      }
      if (opts.requireVideo && !(t.includes('video') || /\.mp4|mime_type=video/i.test(url))) {
        return { ok: false, reason: 'not a video' };
      }
      const length = parseInt(res.headers.get('content-length') || '', 10);
      if (opts.limit && length > opts.limit) return { ok: false, reason: 'too large' };
      const blob = await res.blob();
      if (opts.limit && blob.size > opts.limit) return { ok: false, reason: 'too large' };
      if (opts.minSize && blob.size < opts.minSize) return { ok: false, reason: 'too small' };
      const id = String(++seq);
      window.__scrapemmBlobs[id] = { blob, type: blob.type || type };
      return { ok: true, id, size: blob.size, type: blob.type || type };
    } catch (e) {
      return { ok: false, reason: controller.signal.aborted ? 'timeout' : String(e) };
    } finally {
      if (timer) clearTimeout(timer);
    }
  };
}
"""

_FETCH_JS = "async ({url, opts}) => {" + STASH_JS + " return await window.__scrapemmStash(url, opts); }"

_BLOB_INFO_JS = """id => {
  const entry = (window.__scrapemmBlobs || {})[id];
  return entry ? { size: entry.blob.size, type: entry.type } : null;
}"""

# Native base64 via FileReader: Playwright's evaluate can only return JSON values
_BLOB_READ_JS = """async ({id, start, end}) => {
  const entry = (window.__scrapemmBlobs || {})[id];
  if (!entry) return null;
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(entry.blob.slice(start, end));
  });
  return dataUrl.slice(dataUrl.indexOf(',') + 1);
}"""

_BLOB_DROP_JS = "id => { if (window.__scrapemmBlobs) delete window.__scrapemmBlobs[id]; }"

# Whether the frame replays an archive: its fetch() is rewritten to the archive (wombat)
# or answered by a service worker, so it works for cross-origin media too
_REPLAY_FRAME_JS = """() => !!(window._WB_wombat_location || window.WB_wombat_location
    || window._wb_wombat || window.__WB_replay_top
    || (navigator.serviceWorker && navigator.serviceWorker.controller))"""

# Marks each rendered <img>/<video> with the URL the browser chose for it (`currentSrc`):
# a srcset variant, or, in replay archives, the rewritten replay URL -- which is where
# the browser's copy of the bytes is filed. Runs on a page, frame or element.
_ANNOTATE_JS = """root => {
  const scope = root || document;
  const elements = [...scope.querySelectorAll('img, video')];
  if (root && root.matches && root.matches('img, video')) elements.push(root);
  for (const el of elements) {
    const current = el.currentSrc || '';
    if (!current || current.startsWith('data:') || current.startsWith('blob:')) continue;
    const rendered = el.tagName === 'IMG' ? (el.complete && el.naturalWidth > 0) : el.readyState > 0;
    if (rendered) el.setAttribute('%s', current);
  }
}""" % CURRENT_ATTR

MediaSource = Union[Page, Frame]


async def install_stash(frame: MediaSource) -> None:
    """Makes `window.__scrapemmStash()` available to scripts evaluated in the frame."""
    await frame.evaluate("() => {" + STASH_JS + "}")


async def annotate_rendered_media(target) -> None:
    """Records on each rendered medium which URL the browser loaded it from, see
    `_ANNOTATE_JS`. Best-effort: without it, media are merely fetched once more."""
    with suppress(Exception):
        await target.evaluate(_ANNOTATE_JS)


async def read_stashed(frame: MediaSource, blob_id: str,
                       limit: Optional[int] = None) -> tuple[Optional[bytes], Optional[str]]:
    """Reads a Blob kept by `window.__scrapemmStash()` out of the frame, chunk by chunk,
    and releases it. Returns (content, content type), or (None, None)."""
    try:
        info = await frame.evaluate(_BLOB_INFO_JS, blob_id)
        if not info or not info["size"] or (limit and info["size"] > limit):
            return None, None
        chunks = []
        for start in range(0, info["size"], READ_CHUNK):
            end = min(info["size"], start + READ_CHUNK)
            data = await asyncio.wait_for(frame.evaluate(
                _BLOB_READ_JS, {"id": blob_id, "start": start, "end": end}), timeout=60)
            if data is None:
                return None, None
            chunks.append(base64.b64decode(data))
        return b"".join(chunks), info.get("type")
    except Exception:
        logger.debug(f"Could not read the stashed medium {blob_id}", exc_info=True)
        return None, None
    finally:
        with suppress(Exception):
            await frame.evaluate(_BLOB_DROP_JS, blob_id)


def _header(headers: dict, name: str) -> Optional[str]:
    for key, value in (headers or {}).items():
        if key.lower() == name:
            return value
    return None


def _too_large(headers: dict, limit: Optional[int]) -> bool:
    length = _header(headers, "content-length")
    return bool(limit and length and length.isdigit() and int(length) > limit)


class BrowserMedia:
    """The media of one page, as the browser has them. Create it before navigating, so it
    sees every response the page receives."""

    def __init__(self, page: Page):
        self.page = page
        # Complete (200) media responses by URL -- handles only, the bytes stay in the browser
        self._loaded: dict[str, Response] = {}
        self._session = None
        self._session_lock = asyncio.Lock()
        self._frame_id: Optional[str] = None
        self._replay_frames: dict[MediaSource, bool] = {}
        self._http: Optional[aiohttp.ClientSession] = None  # See `_http_session()`
        self._cookie_hosts: set[str] = set()
        self.stats: Counter = Counter()  # Where the media came from, for the log
        self._last_failure: dict[str, str] = {}  # Why fetching a medium failed, by URL
        try:
            page.on("response", self._record)
        except Exception:
            logger.debug("Cannot observe the page's responses; media will be fetched.")

    def _record(self, response: Response) -> None:
        try:
            if response.status != 200:
                return  # E.g. a video's byte range (206): only part of the medium
            request = response.request
            content_type = response.headers.get("content-type", "")
            if (request.resource_type not in ("image", "media")
                    and not content_type.startswith(("image/", "video/"))):
                return
            self._loaded[response.url] = response
            while (request := request.redirected_from) is not None:
                self._loaded.setdefault(request.url, response)
        except Exception:
            logger.debug("Could not record a response.", exc_info=True)

    async def close(self) -> None:
        """Detaches the page's CDP session, if one was opened. Call before closing the
        page: the Playwright connection is shared and outlives it. Also lets go of the
        page's responses, which the connection would otherwise keep alive."""
        with suppress(Exception):
            self.page.remove_listener("response", self._record)
        self._loaded.clear()
        self._replay_frames.clear()
        session, self._session = self._session, None
        if session is not None:
            with suppress(Exception):
                await asyncio.wait_for(session.detach(), timeout=5)
        http, self._http = self._http, None
        if http is not None:
            with suppress(Exception):
                await http.close()

    async def _http_session(self, url: str) -> aiohttp.ClientSession:
        """A plain HTTP client carrying the browser's user agent and its cookies for
        `url`'s host: for downloads too large to pass through Playwright (see
        `READ_CHUNK`), which Playwright's own request context would do."""
        if self._http is None:
            headers = {k: v for k, v in HEADERS.items() if not k.startswith(("Sec-", "Upgrade"))}
            headers["Accept"] = "*/*"
            with suppress(Exception):
                headers["User-Agent"] = await self.page.evaluate("navigator.userAgent")
            with suppress(Exception):
                headers["Referer"] = self.page.url
            # The browser ignores certificate errors (archives), so this client does too
            self._http = aiohttp.ClientSession(
                headers=headers, cookie_jar=aiohttp.CookieJar(unsafe=True),
                connector=aiohttp.TCPConnector(ssl=False))
        host = urlparse(url).netloc
        if host and host not in self._cookie_hosts:
            self._cookie_hosts.add(host)
            try:
                for cookie in await self.page.context.cookies([url]):
                    domain = cookie["domain"].lstrip(".")
                    self._http.cookie_jar.update_cookies(
                        {cookie["name"]: cookie["value"]},
                        response_url=URL(f"https://{domain}{cookie.get('path') or '/'}"))
            except Exception:
                logger.debug(f"Could not take over the browser's cookies for {host}.",
                             exc_info=True)
        return self._http

    def has_copy(self, url: str) -> bool:
        """Whether the browser holds a complete copy of the medium at `url`."""
        return url in self._loaded

    async def fetch_image(self, url: str, frame: Optional[MediaSource] = None,
                          fallback: Optional[str] = None, source_url: Optional[str] = None,
                          **kwargs) -> Optional[Image]:
        from scrapemm.server.download.images import image_variant
        variant = image_variant(kwargs.get("max_size"))
        ignore_small = kwargs.get("ignore_small_images", True)
        hit, image = await self._kept(url, fallback, variant, ignore_small)
        if hit:
            return image
        content, _ = await self.get(url, frame, limit=MAX_IMAGE_BYTES, fallback=fallback)
        if content:
            # CPU-bound; would otherwise stall every other retrieval on the event loop
            image = await decode_image(content, source_url or url, **kwargs)
            await self._keep(url, fallback, image, variant)
            return image
        return None

    async def fetch_video(self, url: str, frame: Optional[MediaSource] = None,
                          fallback: Optional[str] = None, max_size: Optional[int] = None,
                          source_url: Optional[str] = None,
                          timeout: float = 180.0) -> Optional[Video]:
        limit = max_size or MAX_VIDEO_BYTES
        hit, video = await self._kept(url, fallback, max_bytes=limit)
        if hit and video is not None:
            return video
        video = await self._fetch_video(url, frame, fallback, max_size, source_url, timeout)
        if video is not None:
            await self._keep(url, fallback, video)
        return video

    async def _fetch_video(self, url: str, frame: Optional[MediaSource], fallback: Optional[str],
                           max_size: Optional[int], source_url: Optional[str],
                           timeout: float) -> Optional[Video]:
        limit = max_size or MAX_VIDEO_BYTES
        content, content_type = await self.get(url, frame, limit, fallback, timeout)

        if content is None and (capture := await _nearest_wayback_video(self.page, url)):
            logger.debug(f"{url} does not replay; using the capture {capture} instead.")
            content, content_type = await self.get(capture, frame, limit, timeout=timeout)

        # HLS playlists are plain text manifests, not raw video: remux via ffmpeg. The
        # segments are downloaded directly, with the browser's cookies: through
        # Playwright's request context, each segment blocked the event loop (READ_CHUNK)
        if content_type and is_hls(content_type) and not url.startswith(BLOB_SCHEME):
            return await download_hls_video(url, session=await self._http_session(url),
                                            max_video_size=max_size)

        if not content:
            return None
        # Writes the file: off the event loop
        return await asyncio.to_thread(video_from_binary, content, source_url or url)

    async def get(self, url: str, frame: Optional[MediaSource] = None,
                  limit: Optional[int] = None, fallback: Optional[str] = None,
                  timeout: float = 180.0) -> tuple[Optional[bytes], Optional[str]]:
        """Returns (content, content type) of the medium at `url`, or (None, None).
        `fallback` is another URL of the same medium, typically the one the browser
        rendered (`CURRENT_ATTR`). For each URL, the browser's own copy comes first, then
        a fetch through the browser.

        On a live page, `url` is fetched before the fallback's copy is taken: the two are
        usually different srcset variants, and `url` is the larger one. In a replay
        archive, both name the same archived file, so any copy the browser has is taken
        before anything is fetched."""
        found = await self._get(url, frame, limit, fallback, timeout)
        if not found[0]:
            # An archived capture's result without it is not kept for good
            from scrapemm.server.cache import note_failed_medium
            for candidate in (url, fallback):
                if candidate:
                    note_failed_medium(candidate, self._page_url())
        return found

    def _page_url(self) -> Optional[str]:
        return getattr(self.page, "url", None)

    async def _kept(self, url: str, fallback: Optional[str], variant: str = "",
                    ignore_small: bool = True, max_bytes: Optional[int] = None):
        """An archived medium's registry item from the scrapeMM cache (see
        `ImmutableCache.item()`): (True, item or None) on a hit, (False, None) otherwise."""
        from scrapemm.server.cache import immutable
        for candidate in (url, fallback):
            if candidate:
                hit, item = await immutable.item(candidate, self._page_url(), variant,
                                                 ignore_small, max_bytes)
                if hit:
                    self.stats["cache"] += 1
                    return True, item
        return False, None

    async def _keep(self, url: str, fallback: Optional[str], item, variant: str = "") -> None:
        from scrapemm.server.cache import immutable
        for candidate in (url, fallback):
            if candidate:
                await immutable.keep_item(candidate, item, self._page_url(), variant)

    async def _get(self, url: str, frame: Optional[MediaSource], limit: Optional[int],
                   fallback: Optional[str], timeout: float) -> tuple[Optional[bytes], Optional[str]]:
        timeout = budget.cap(timeout)  # No longer than the retrieval has time for
        steps = [(self._copy, url), (self._fetch, url)]
        if fallback and fallback != url:
            steps += [(self._copy, fallback), (self._fetch, fallback)]
            if frame is not None and await self._is_replay_frame(frame):
                steps = steps[0::2] + steps[1::2]  # All copies first

        # `timeout` bounds the medium as a whole, not each step: the steps' own timeouts
        # added up, and a stream that kept trickling (thequint.com's ad video) held the
        # retrieval until the browser's 10-minute limit failed the entire page
        try:
            async with asyncio.timeout(timeout):
                for step, candidate in steps:
                    if found := await step(candidate, frame, limit, timeout):
                        return found
        except TimeoutError:
            self._last_failure[url] = f"no answer within {timeout:.0f} s"

        # Visible at info level: a medium silently missing from a result is hard to trace
        # (archive.org once stopped serving an image its index still listed, HTTP 404)
        reason = self._last_failure.get(url) or (fallback and self._last_failure.get(fallback))
        logger.info(f"Dropped the medium {url[:150]}: "
                    f"{reason or 'the browser has no copy and could not fetch it'}.")
        self.stats["failed"] += 1
        return None, None

    async def _copy(self, url: str, frame: Optional[MediaSource], limit: Optional[int],
                    _timeout: float) -> Optional[tuple[bytes, Optional[str]]]:
        """A copy the browser holds already: a Blob stashed in the frame, or a response
        the page received."""
        if url.startswith(BLOB_SCHEME):
            if frame is None:
                return None
            content, content_type = await read_stashed(frame, url[len(BLOB_SCHEME):], limit)
            if not content:
                return None
            self.stats["stashed"] += 1
            return content, content_type
        if found := await self._from_loaded(url, limit):
            self.stats["loaded"] += 1
        return found

    async def _from_loaded(self, url: str, limit: Optional[int]) -> Optional[tuple[bytes, Optional[str]]]:
        """The bytes of a response the page already received. If the browser is still
        receiving it, waits for that instead of starting a second download."""
        response = self._loaded.get(url)
        if response is None or _too_large(response.headers, limit):
            return None
        # One message: only for bodies known to be small (see MAX_BODY_MESSAGE). Images
        # of unknown size are small in practice; a video of unknown size is not.
        length = _header(response.headers, "content-length")
        if length and length.isdigit():
            if int(length) > MAX_BODY_MESSAGE:
                return None
        elif not (response.headers.get("content-type", "").startswith("image/")
                  or response.request.resource_type == "image"):
            return None
        try:
            body = await asyncio.wait_for(response.body(), timeout=LOADED_BODY_TIMEOUT)
        except Exception as e:
            logger.debug(f"The browser's copy of {url} is not available: {e}")
            return None
        if not body or (limit and len(body) > limit):
            return None
        return body, response.headers.get("content-type")

    async def _fetch(self, url: str, frame: Optional[MediaSource], limit: Optional[int],
                     timeout: float) -> Optional[tuple[bytes, Optional[str]]]:
        """Fetches through the browser. In-frame first where that works: in a replay
        frame (whose service worker or rewriting only in-frame requests reach) or for
        same-origin media. Elsewhere, an in-frame fetch fails on CORS for nearly every
        cross-origin medium, so the browser's network stack goes first."""
        if url.startswith(BLOB_SCHEME):
            return None  # Exists only inside the frame, see `_copy()`
        strategies = [("network", self._from_network), ("http", self._from_http)]
        if frame is not None:
            in_frame = ("frame", lambda u, l, t: self._from_frame(frame, u, l, t))
            first = _same_origin(url, frame.url) or await self._is_replay_frame(frame)
            strategies.insert(0 if first else 1, in_frame)
        for name, strategy in strategies:
            if found := await strategy(url, limit, timeout):
                self.stats[name] += 1
                return found
        return None

    async def _is_replay_frame(self, frame: MediaSource) -> bool:
        if frame not in self._replay_frames:
            try:
                self._replay_frames[frame] = bool(await frame.evaluate(_REPLAY_FRAME_JS))
            except Exception:
                self._replay_frames[frame] = False
        return self._replay_frames[frame]

    async def _from_frame(self, frame: MediaSource, url: str, limit: Optional[int],
                          timeout: float) -> Optional[tuple[bytes, Optional[str]]]:
        try:
            result = await asyncio.wait_for(
                frame.evaluate(_FETCH_JS, {"url": url, "opts": {"limit": limit}}), timeout=timeout)
        except Exception as e:
            logger.debug(f"In-frame fetch of {url} failed: {e}")
            return None
        if not result or not result.get("ok"):
            logger.debug(f"In-frame fetch of {url} failed: {(result or {}).get('reason')}")
            return None
        content, content_type = await read_stashed(frame, result["id"], limit)
        return (content, content_type or result.get("type")) if content else None

    async def _cdp(self):
        """The page's own CDP session (lazily), plus the id of its main frame."""
        async with self._session_lock:
            if self._session is None:
                session = await self.page.context.new_cdp_session(self.page)
                # In Chromium, the id of a page's main frame is its target id
                self._frame_id = (getattr(self.page, "_scrapemm_target_id", None)
                                  or (await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"])
                self._session = session
            return self._session

    async def _from_network(self, url: str, limit: Optional[int],
                            timeout: float) -> Optional[tuple[bytes, Optional[str]]]:
        """`Network.loadNetworkResource`: the browser's own network stack -- its TLS
        fingerprint, cookies and HTTP cache -- without CORS, streamed in chunks. Bypasses
        service workers, so it cannot replay archived media."""
        for attempt in range(len(THROTTLE_BACKOFF) + 1):
            try:
                session = await self._cdp()
                resource = (await asyncio.wait_for(session.send("Network.loadNetworkResource", {
                    "frameId": self._frame_id, "url": url,
                    "options": {"disableCache": False, "includeCredentials": True},
                }), timeout=timeout))["resource"]
            except Exception as e:
                logger.debug(f"Browser network fetch of {url} failed: {e}")
                self._last_failure[url] = f"{type(e).__name__}: {e}"
                return None
            status = resource.get("httpStatusCode")
            if status not in THROTTLE_STATUSES or attempt == len(THROTTLE_BACKOFF):
                break
            if stream := resource.get("stream"):
                with suppress(Exception):
                    await session.send("IO.close", {"handle": stream})
            logger.info(f"{urlparse(url).netloc} throttles media (HTTP {status}); retrying "
                        f"{url[:120]} in {THROTTLE_BACKOFF[attempt]:.0f} s.")
            await asyncio.sleep(THROTTLE_BACKOFF[attempt])

        stream = resource.get("stream")
        headers = resource.get("headers") or {}
        try:
            if not resource.get("success") or not stream:
                reason = f"HTTP {resource.get('httpStatusCode')} {resource.get('netErrorName', '')}"
                logger.debug(f"Browser network fetch of {url} failed: {reason}")
                self._last_failure[url] = reason.strip()
                return None
            if _too_large(headers, limit):
                return None
            chunks, size = [], 0
            while True:
                chunk = await asyncio.wait_for(
                    session.send("IO.read", {"handle": stream, "size": READ_CHUNK}), timeout=60)
                data = chunk.get("data", "")
                data = base64.b64decode(data) if chunk.get("base64Encoded") else data.encode("latin-1")
                chunks.append(data)
                size += len(data)
                if limit and size > limit:
                    return None
                if chunk.get("eof"):
                    break
            return (b"".join(chunks), _header(headers, "content-type")) if size else None
        except Exception as e:
            logger.debug(f"Reading {url} from the browser failed: {e}")
            return None
        finally:
            if stream:
                with suppress(Exception):
                    await session.send("IO.close", {"handle": stream})

    async def _from_http(self, url: str, limit: Optional[int],
                         timeout: float) -> Optional[tuple[bytes, Optional[str]]]:
        """A direct download with the browser's user agent and cookies: the last resort,
        as it is not the browser's network stack. Replaces Playwright's request context,
        whose bodies pass through its pipe in one message (see `READ_CHUNK`)."""
        try:
            session = await self._http_session(url)
            for attempt in range(len(THROTTLE_BACKOFF) + 1):
                async with session.get(url, timeout=MEDIA_TIMEOUT, allow_redirects=True) as response:
                    if response.status in THROTTLE_STATUSES and attempt < len(THROTTLE_BACKOFF):
                        logger.info(f"{urlparse(url).netloc} throttles media (HTTP "
                                    f"{response.status}); retrying {url[:120]} in "
                                    f"{THROTTLE_BACKOFF[attempt]:.0f} s.")
                        await asyncio.sleep(THROTTLE_BACKOFF[attempt])
                        continue
                    if response.status != 200:
                        self._last_failure[url] = f"HTTP {response.status}"
                        return None
                    if _too_large(dict(response.headers), limit):
                        return None
                    content = await asyncio.wait_for(stream(response, max_size=limit), timeout=timeout)
                    if not content:
                        return None
                    return content, response.headers.get("content-type")
            return None
        except Exception as e:
            logger.debug(f"Direct download of {url} failed: {type(e).__name__}: {e}")
            self._last_failure[url] = f"{type(e).__name__}: {e}"
            return None


# Answers that mean "not now" rather than "not at all": retried after these pauses
# (archive.org answers 429 once a client fetches too much, and 503 while overloaded)
THROTTLE_STATUSES = {429, 503}
THROTTLE_BACKOFF = (2.0, 6.0)


def _same_origin(url: str, other: str) -> bool:
    try:
        a, b = urlparse(url), urlparse(other)
        return (a.scheme, a.netloc) == (b.scheme, b.netloc)
    except ValueError:
        return False


_WAYBACK_URL = re.compile(r"^https?://web\.archive\.org/web/(\d{4,14})[a-z_]*/(https?://.+)$")

# A path segment this long is a content id or hash, so the path alone names one file
_UNIQUE_SEGMENT = re.compile(r"/[A-Za-z0-9_\-]{20,}(/|$)")


async def _nearest_wayback_video(page: Page, url: str) -> Optional[str]:
    """For a Wayback video URL that does not replay, the nearest complete capture of
    the same file, as a raw (`id_`) URL.

    The Wayback Machine looks media up by their exact URL. Video CDN URLs carry
    expiring query parameters, so the one in the page is often not the one that was
    captured -- or it was, but only as a partial (206) response, which does not replay.
    The capture index, queried by the path alone, knows the other captures.
    """
    match = _WAYBACK_URL.match(url)
    if not match:
        return None
    timestamp, original = match.groups()
    path = original.split("?", 1)[0]
    # Only a path that identifies one file is safe to match without its query: for
    # something like /video.php?id=5 it would find every video the endpoint serves.
    if not _UNIQUE_SEGMENT.search(urlparse(path).path):
        return None

    query = urlencode({"url": path, "matchType": "prefix", "output": "json",
                       "fl": "timestamp,statuscode,mimetype,original", "limit": "200"})
    try:
        response = await page.context.request.get(
            f"https://web.archive.org/cdx/search/cdx?{query}", timeout=30_000)
        rows = (await response.json())[1:] if response.ok else []
    except Exception:
        logger.debug(f"Wayback capture lookup failed for {path}", exc_info=True)
        return None

    complete = [(ts, orig) for ts, status, mime, orig in rows
                if status == "200" and mime.startswith("video/")]
    if not complete:
        return None
    ts, orig = min(complete, key=lambda c: abs(int(c[0].ljust(14, "0")) -
                                               int(timestamp.ljust(14, "0"))))
    return f"https://web.archive.org/web/{ts}id_/{orig}"
