import asyncio
import html as html_lib
import json
import re
from contextlib import suppress
import logging
import time
from typing import Optional

from playwright.async_api import TimeoutError, Page, Frame, Error as PlaywrightError

from scrapemm.common import RetrievalFailed
from scrapemm.common.exceptions import TargetUnavailableError
from scrapemm.server.integrations.headed_browser import HeadedBrowser, ContentTarget, settle_dom
from scrapemm.server.download.browser import (BLOB_ATTR, MAX_VIDEO_BYTES, install_stash,
                                              _WAYBACK_URL)
from scrapemm.server.integrations.perma_cc import _stash_media_in_frame

logger = logging.getLogger("scrapeMM")

_PLAYBACK_IFRAME = "#playback iframe, iframe#playback"

# TikTok's login-page placeholder video, which must never count as the content
_VIDEO_DECOYS = ("playback1.mp4", "ttwstatic.com", "webapp-desktop/playback")

# Once the player fetched a video, how long its <video> element still gets to appear
SERVED_VIDEO_GRACE = 5.0
# How long a video page whose video was not collected gets once more, after a reload
# (see `_collect_video()`)
SECOND_VIDEO_WAIT_MS = 30_000

# What the page looks like when its video could not be collected, for the log
_PAGE_STATE_JS = """() => ({
    visibility: document.visibilityState, width: window.innerWidth,
    text: document.body ? document.body.innerText.length : 0,
    videos: document.querySelectorAll('video').length,
    images: [...document.images].filter(i => i.naturalWidth > 0).length})"""


async def _stash_video(frame: Frame, url: str) -> str:
    """Fetches the video at `url` inside the frame (where the archive's session and
    same-origin rules apply) and marks the page's <video> -- or a new one -- with the
    resulting Blob (see `_stash_media_in_frame()`). Returns 'ok' or why not."""
    await install_stash(frame)
    return await frame.evaluate(
        """async ({url, limit, attr}) => {
            const res = await window.__scrapemmStash(url, { limit, requireVideo: true, minSize: 10000 });
            if (!res.ok) return res.reason;
            let video = document.querySelector('video');
            if (!video) {
                video = document.createElement('video');
                document.body.appendChild(video);
            }
            video.querySelectorAll('source').forEach(s => s.remove());
            video.setAttribute(attr, res.id);
            return 'ok';
        }""", {"url": url, "limit": MAX_VIDEO_BYTES, "attr": BLOB_ATTR})


# Where a captured page names its video, most reliable first: the Open Graph video, the
# page's own structured data (JSON-LD `contentUrl`: Kwai, Instagram, X, many news sites),
# TikTok's hydration data, and a plain <video>/<source> in the server-rendered markup
_RAW_VIDEO_PATTERNS = [
    (re.compile(r'<meta[^>]+(?:property|name)="(?:og:video(?::secure_url|:url)?|twitter:player:stream)"'
                r'[^>]+content="([^"]+)"', re.I), "html"),
    (re.compile(r'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="og:video(?::secure_url|:url)?"',
                re.I), "html"),
    (re.compile(r'"contentUrl"\s*:\s*"((?:[^"\\]|\\.)+)"'), "json"),
    (re.compile(r'"(?:playAddr|downloadAddr|video_url|videoUrl|playbackUrl)"\s*:\s*"((?:[^"\\]|\\.)+)"'),
     "json"),
    (re.compile(r'<(?:video|source)[^>]+src="([^"]+)"', re.I), "html"),
]
# At most this many named videos are tried: the first ones are the page's own
MAX_RAW_VIDEOS = 4


def _raw_video_candidates(markup: str) -> list[str]:
    """The video URLs a captured page's own markup names, in the order of
    `_RAW_VIDEO_PATTERNS`, without decoys and duplicates."""
    found: list[str] = []
    for pattern, kind in _RAW_VIDEO_PATTERNS:
        for match in pattern.findall(markup):
            try:
                url = json.loads(f'"{match}"') if kind == "json" else html_lib.unescape(match)
            except ValueError:
                continue
            if url.startswith("//"):
                url = "https:" + url
            if archived := _WAYBACK_URL.match(url):
                url = archived.group(2)  # Already rewritten to a replay URL: the original
            if (url.startswith(("http://", "https://")) and url not in found
                    and not any(decoy in url.lower() for decoy in _VIDEO_DECOYS)):
                found.append(url)
    return found[:MAX_RAW_VIDEOS]


async def _video_from_raw_capture(page: Page, frame: Frame) -> bool:
    """The last resort for a video page whose replayed player yielded no video: the video
    its archived markup names, fetched from the Wayback Machine directly.

    The markup is the capture as the archive stored it (`id_` mode), before any script
    ran -- so neither a replayed app that went wrong nor a player that never mounted
    matters. Each named video is fetched as captured at the page's time (the archive
    redirects to the nearest capture of it), or else, since video URLs carry expiring
    signatures that seldom match a capture exactly, as the nearest capture of the same
    file (looked up by its path, see `_captured_video()`)."""
    match = _WAYBACK_URL.match(page.url or "")
    if not match:
        return False
    timestamp, original = match.groups()
    raw_url = f"https://web.archive.org/web/{timestamp}id_/{original}"
    try:
        markup = await frame.evaluate(
            """async url => {
                try {
                    const res = await fetch(url, { credentials: 'include' });
                    return res.ok ? await res.text() : null;
                } catch (e) { return null; }
            }""", raw_url)
    except PlaywrightError:
        markup = None
    if not markup:
        logger.info(f"Could not read the raw capture {raw_url}.")
        return False

    candidates = _raw_video_candidates(markup)
    for video in candidates:
        attempts = [f"https://web.archive.org/web/{timestamp}id_/{video}"]
        for url in attempts:
            result = await _stash_video(frame, url)
            if result == "ok":
                logger.info(f"Took the video of {page.url} from its capture's markup: {url}")
                return True
            logger.debug(f"The video {url} named in the capture could not be fetched: {result}")
        if nearest := await _captured_video(page, timestamp, video):
            if await _stash_video(frame, nearest) == "ok":
                logger.info(f"Took the video of {page.url} from its capture's markup: {nearest}")
                return True
    logger.info(f"The capture's markup at {raw_url} names {len(candidates)} video(s); "
                f"none could be fetched.")
    return False


async def _captured_video(page: Page, timestamp: str, video: str) -> Optional[str]:
    """The Wayback URL (`id_`) of the capture of `video` nearest to `timestamp`, looked up
    in the capture index by the video's path alone -- its query carries signatures that
    change with every request, so the exact URL is rarely the captured one. Players fetch
    videos in byte ranges, so a 206 capture counts too (a Kwai video was archived only as
    one). Only for paths long enough to name one file (a content id or hash)."""
    from urllib.parse import urlencode, urlsplit
    path = video.split("?", 1)[0]
    if len(urlsplit(path).path) < 24:
        return None
    query = urlencode({"url": path, "matchType": "prefix", "output": "json",
                       "fl": "timestamp,statuscode,mimetype,original", "limit": "100"})
    rows = None
    # The capture index is often slow or briefly "Temporarily Offline": a second try
    for attempt in range(2):
        try:
            response = await page.context.request.get(
                f"https://web.archive.org/cdx/search/cdx?{query}", timeout=45_000)
            if response.ok:
                rows = (await response.json())[1:]
                break
        except Exception as e:
            logger.debug(f"Wayback capture lookup failed for {path}: {type(e).__name__}: {e}")
        await asyncio.sleep(2)
    if rows is None:
        logger.info(f"The Wayback Machine's capture index did not answer for {path}.")
        return None
    captures = [(ts, orig) for ts, status, mime, orig in rows
                if status in ("200", "206") and mime.startswith("video/")]
    if not captures:
        return None
    ts, orig = min(captures, key=lambda c: abs(int(c[0].ljust(14, "0")) - int(timestamp.ljust(14, "0"))))
    return f"https://web.archive.org/web/{ts}id_/{orig}"


async def _page_state(frame: Frame) -> dict:
    try:
        return await frame.evaluate(_PAGE_STATE_JS)
    except PlaywrightError:
        return {}


async def _collected_video(frame: Frame) -> bool:
    """Whether the frame holds a video that `resolve_media()` will take: one fetched into
    the page already (BLOB_ATTR), or one with a source that is not a decoy."""
    try:
        return bool(await frame.evaluate(
            """({attr, decoys}) => [...document.querySelectorAll('video, video source')].some(v => {
                if (v.hasAttribute(attr)) return true;
                const src = (v.getAttribute('src') || '').toLowerCase();
                return !!src && !src.startsWith('blob:') && !decoys.some(d => src.includes(d));
            })""", {"attr": BLOB_ATTR, "decoys": list(_VIDEO_DECOYS)}))
    except PlaywrightError:
        return False


class ArchiveOrg(HeadedBrowser):
    """Integration for retrieving content from archive.org (Internet Archive)."""
    name = "Internet Archive"
    domains = ["archive.org"]

    def _watch_page(self, page: Page) -> None:
        """Records the video responses the archive actually served while the page loaded.

        The URL in a <video> element is often not the one that was archived: TikTok's
        carry expiring signatures, so the Wayback Machine answers 404 for the rewritten
        attribute while its player fetched the video under a different, archived URL.
        Those responses are the only reliable record of where the video really is.
        """
        videos: list[tuple[int, str]] = []

        def on_response(response) -> None:
            try:
                if response.status not in (200, 206):
                    return
                url = response.url
                content_type = response.headers.get("content-type", "")
                if not (content_type.startswith("video/") or "mime_type=video" in url):
                    return
                if any(decoy in url.lower() for decoy in _VIDEO_DECOYS):
                    return
                # A range response names the full size after the slash
                total = response.headers.get("content-range", "").rpartition("/")[2]
                size = int(total) if total.isdigit() else int(
                    response.headers.get("content-length") or 0)
                videos.append((size, url))
            except Exception:
                logger.debug("Could not inspect a response.", exc_info=True)

        page.on("response", on_response)
        page._scrapemm_videos = videos  # Read back in _extract_content

    async def _inline_served_video(self, frame: Frame, page: Page) -> None:
        """Falls back to the video the page's own player loaded, if no other video was
        fetched. Fetched inside the frame, so the archive's session applies; the element
        is marked with the resulting Blob (see `_stash_media_in_frame()`)."""
        videos = getattr(page, "_scrapemm_videos", [])
        if not videos:
            return
        try:
            await install_stash(frame)
            fetched = await frame.evaluate(
                """attr => [...document.querySelectorAll('video, video source')]
                    .some(v => v.hasAttribute(attr))""", BLOB_ATTR)
            if fetched:
                return
            size, url = max(videos)  # The largest is the content, not a preview
            result = await _stash_video(frame, url)
            if result == "ok":
                logger.debug(f"Fetched the video the archive served at {url}.")
            else:
                logger.warning(f"Could not fetch the archived video {url}: {result}")
        except Exception as e:
            logger.warning(f"Could not fetch the archived video: {type(e).__name__}: {e}")

    async def _settle_after_goto(self, page: Page) -> None:
        """Wait until it is clear how the snapshot is replayed: in a playback iframe, or
        (the common case) rewritten into the top-level document. The latter is marked by
        the Wayback Machine's replay script or toolbar, so it needs no waiting for an
        iframe that will never come -- which used to cost the full timeout every time."""
        try:
            await page.wait_for_function(
                f"""() => !!document.querySelector({_PLAYBACK_IFRAME!r})
                    || !!window.__wm || !!document.getElementById('wm-ipp-base')""",
                timeout=8000)
        except TimeoutError:
            pass

    @staticmethod
    async def _wait_playback_frame_ready(frame: Frame) -> None:
        """Waits until the replayed page has finished building itself. It used to count
        as ready after two equal size samples 100 ms apart, which under load came long
        before the archived player had mounted: the Kwai video in the test suite was then
        missing from the result (18 media collected instead of 68). The shared DOM settle
        wants a full second of stillness (at most DOM_SETTLE_TIMEOUT)."""
        await settle_dom(frame)

    @staticmethod
    def _url_suggests_primary_video(url: str) -> bool:
        u = (url or "").lower()
        return "/video/" in u or "tiktok.com" in u or "kwai.com" in u

    @staticmethod
    async def _frame_has_primary_video(frame: Frame) -> bool:
        try:
            return bool(
                await frame.evaluate(
                    """() => {
                        const decoy = (s) => {
                            const u = (s || '').toLowerCase();
                            return u.includes('playback1.mp4')
                                || u.includes('ttwstatic.com')
                                || u.includes('webapp-desktop/playback');
                        };
                        for (const v of document.querySelectorAll('video')) {
                            const src = v.getAttribute('src') || '';
                            const cur = v.currentSrc || '';
                            if ((src || cur) && !decoy(src) && !decoy(cur)) return true;
                            for (const s of v.querySelectorAll('source[src]')) {
                                const u = s.getAttribute('src') || '';
                                if (u && !decoy(u)) return true;
                            }
                        }
                        return false;
                    }"""
                )
            )
        except Exception:
            return False

    async def _wait_for_primary_video(self, page: Page, preferred: Frame | None = None,
                                      timeout_ms: int = 30000) -> Frame:
        """Wait for a late-mounted content <video> across frames (TikTok SPA).

        TikTok's archived shell already has enough text/images for `_wait_playback_frame_ready`
        to return, while the real player only mounts ~10–15s later. A login-page decoy
        (`playback1.mp4` on ttwstatic) must not count as success.

        Returns the frame that contains the video (or `preferred` / main on timeout).
        """
        deadline = time.monotonic() + timeout_ms / 1000
        fallback = preferred or page.main_frame
        served = getattr(page, "_scrapemm_videos", [])
        served_since = None
        while time.monotonic() < deadline:
            frames = []
            if preferred is not None:
                frames.append(preferred)
            for f in page.frames:
                if f not in frames:
                    frames.append(f)
            for frame in frames:
                if await self._frame_has_primary_video(frame):
                    return frame
            if served:
                # The player fetched the real video. Its element usually follows at once;
                # if it does not, `_inline_served_video()` fetches what was served.
                served_since = served_since or time.monotonic()
                if time.monotonic() - served_since >= SERVED_VIDEO_GRACE:
                    return fallback
            await asyncio.sleep(0.25)
        logger.debug("Archive.org primary video did not appear before timeout; continuing.")
        return fallback

    async def _collect_video(self, page: Page, frame: Frame) -> Frame:
        """Waits for the replayed player's video and collects the frame's media. If no
        video was collected, the page is loaded once more and the same is done again.

        Waiting longer does not help then: the replay did not go wrong slowly, it went
        wrong differently. The stored results of failed production runs show the TikTok
        capture rendered as the app's empty skeleton ("Discover" and blank headings in
        place of the post, 2.6 of 6.3k characters) -- the replayed app had thrown away
        the post it was served, most likely because the Wayback Machine answered one of
        its script requests from another capture (it picks the nearest capture per
        request, which varies). A new navigation resolves them afresh."""
        for attempt in range(2):
            frame = await self._wait_for_primary_video(
                page, preferred=frame, timeout_ms=30000 if attempt == 0 else SECOND_VIDEO_WAIT_MS)
            await _stash_media_in_frame(frame)
            await self._inline_served_video(frame, page)
            if await _collected_video(frame):
                return frame
            state = await _page_state(frame)
            if attempt == 0:
                logger.info(f"No video collected at {page.url} ({state}); loading the "
                            f"capture once more.")
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=60_000)
                except PlaywrightError:
                    logger.debug(f"Reloading {page.url} failed.", exc_info=True)
                    break
                await self._settle_after_goto(page)
                frame = await self._replay_frame(page)
                await self._wait_playback_frame_ready(frame)
            else:
                logger.info(f"The archived player at {page.url} yielded no video ({state}); "
                            f"looking in the capture's markup.")
                await _video_from_raw_capture(page, frame)
        return frame

    @staticmethod
    async def _replay_frame(page: Page) -> Frame:
        """The frame that holds the replayed page: the playback iframe, if there is one."""
        with suppress(PlaywrightError):
            if element := await page.query_selector(_PLAYBACK_IFRAME):
                if frame := await element.content_frame():
                    return frame
        return page.main_frame

    async def _extract_content(self, page: Page) -> Optional[ContentTarget]:
        if "503 Service Unavailable".lower() in (await page.content()).lower():
            raise TargetUnavailableError("Archive.org is currently unavailable (Error 503).")

        wants_video = self._url_suggests_primary_video(page.url or "")

        # Selector was already awaited in _settle_after_goto — no second long wait.
        try:
            playback_iframe = await page.query_selector(_PLAYBACK_IFRAME)
            if playback_iframe:
                frame = await playback_iframe.content_frame()
                if frame:
                    await self._wait_playback_frame_ready(frame)
                    if wants_video:
                        return await self._collect_video(page, frame)
                    await _stash_media_in_frame(frame)
                    return frame

            # Rewritten snapshot without playback iframe (content already on the top frame).
            target: Frame = page.main_frame
            if wants_video:
                await self._wait_playback_frame_ready(target)
                return await self._collect_video(page, target)
            return page

        except PlaywrightError:
            raise RetrievalFailed("Archive.org playback iframe not loaded successfully.")
