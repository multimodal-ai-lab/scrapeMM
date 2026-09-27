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

from ezmm import Image, Video
from playwright.async_api import Page, Frame, Response

from scrapemm.server.download.images import image_from_binary
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
# every single message, however large the video
READ_CHUNK = 8 * 1024 * 1024

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
    try {
      const res = await fetch(url, { credentials: 'include' });
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
      return { ok: false, reason: String(e) };
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
        self.stats: Counter = Counter()  # Where the media came from, for the log
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
        page: the Playwright connection is shared and outlives it."""
        session, self._session = self._session, None
        if session is not None:
            with suppress(Exception):
                await asyncio.wait_for(session.detach(), timeout=5)

    def has_copy(self, url: str) -> bool:
        """Whether the browser holds a complete copy of the medium at `url`."""
        return url in self._loaded

    async def fetch_image(self, url: str, frame: Optional[MediaSource] = None,
                          fallback: Optional[str] = None, source_url: Optional[str] = None,
                          **kwargs) -> Optional[Image]:
        content, _ = await self.get(url, frame, limit=MAX_IMAGE_BYTES, fallback=fallback)
        if content:
            # CPU-bound; would otherwise stall every other retrieval on the event loop
            return await asyncio.to_thread(image_from_binary, content,
                                           source_url=source_url or url, **kwargs)
        return None

    async def fetch_video(self, url: str, frame: Optional[MediaSource] = None,
                          fallback: Optional[str] = None, max_size: Optional[int] = None,
                          source_url: Optional[str] = None,
                          timeout: float = 180.0) -> Optional[Video]:
        limit = max_size or MAX_VIDEO_BYTES
        content, content_type = await self.get(url, frame, limit, fallback, timeout)

        if content is None and (capture := await _nearest_wayback_video(self.page, url)):
            logger.debug(f"{url} does not replay; using the capture {capture} instead.")
            content, content_type = await self.get(capture, frame, limit, timeout=timeout)

        # HLS playlists are plain text manifests, not raw video: remux via ffmpeg,
        # reusing the shared request context so segment downloads stay authenticated
        if content_type and is_hls(content_type) and not url.startswith(BLOB_SCHEME):
            return await download_hls_video(url, session=self.page.context.request)

        return video_from_binary(content, source_url=source_url or url) if content else None

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
        steps = [(self._copy, url), (self._fetch, url)]
        if fallback and fallback != url:
            steps += [(self._copy, fallback), (self._fetch, fallback)]
            if frame is not None and await self._is_replay_frame(frame):
                steps = steps[0::2] + steps[1::2]  # All copies first

        for step, candidate in steps:
            if found := await step(candidate, frame, limit, timeout):
                return found

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
        strategies = [("network", self._from_network), ("context", self._from_request_context)]
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
        try:
            session = await self._cdp()
            resource = (await asyncio.wait_for(session.send("Network.loadNetworkResource", {
                "frameId": self._frame_id, "url": url,
                "options": {"disableCache": False, "includeCredentials": True},
            }), timeout=timeout))["resource"]
        except Exception as e:
            logger.debug(f"Browser network fetch of {url} failed: {e}")
            return None

        stream = resource.get("stream")
        headers = resource.get("headers") or {}
        try:
            if not resource.get("success") or not stream:
                logger.debug(f"Browser network fetch of {url} failed: HTTP "
                             f"{resource.get('httpStatusCode')} {resource.get('netErrorName', '')}")
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

    async def _from_request_context(self, url: str, limit: Optional[int],
                                    timeout: float) -> Optional[tuple[bytes, Optional[str]]]:
        """Playwright's own HTTP client with the browser context's cookies: the last
        resort, as it is not the browser's network stack."""
        try:
            response = await self.page.context.request.get(url, timeout=timeout * 1000)
            if not response.ok or _too_large(response.headers, limit):
                return None
            content = await response.body()
            if not content or (limit and len(content) > limit):
                return None
            return content, response.headers.get("content-type")
        except Exception as e:
            logger.debug(f"Request-context fetch of {url} failed: {e}")
            return None


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
