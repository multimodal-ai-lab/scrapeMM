import asyncio
import logging
import time
from typing import Optional

from playwright.async_api import TimeoutError, Page, Frame, Error as PlaywrightError

from scrapemm.common import RetrievalFailed
from scrapemm.common.exceptions import TargetUnavailableError
from scrapemm.server.integrations.headed_browser import HeadedBrowser, ContentTarget
from scrapemm.server.download.browser import BLOB_ATTR, MAX_VIDEO_BYTES, install_stash
from scrapemm.server.integrations.perma_cc import _stash_media_in_frame

logger = logging.getLogger("scrapeMM")

_PLAYBACK_IFRAME = "#playback iframe, iframe#playback"

# TikTok's login-page placeholder video, which must never count as the content
_VIDEO_DECOYS = ("playback1.mp4", "ttwstatic.com", "webapp-desktop/playback")


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
            result = await frame.evaluate(
                """async ({url, limit, attr}) => {
                    const res = await window.__scrapemmStash(url, { limit });
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

    async def _wait_playback_frame_ready(self, frame: Frame, timeout_ms: int = 15000) -> None:
        """Return as soon as the archived document has usable content and the DOM is stable.

        Avoids long waits on load/networkidle (Wayback keeps analytics/beacon traffic alive)
        while still not proceeding before the archived body is present.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        previous_size = -1
        stable_checks = 0

        while time.monotonic() < deadline:
            try:
                info = await frame.evaluate(
                    """() => {
                        if (!document.body || document.readyState === 'loading') {
                            return { ready: false, size: 0 };
                        }
                        const size = document.documentElement
                            ? document.documentElement.outerHTML.length
                            : 0;
                        // Wayback-rewritten assets/links, or any primary media/content root.
                        const hasArchived = !!document.querySelector(
                            'img[src*="/web/"], video[src*="/web/"], source[src*="/web/"], a[href*="/web/"]'
                        );
                        const hasMedia = !!document.querySelector(
                            'img[src], video[src], video source[src], article, main, [role="main"]'
                        );
                        const textLen = (document.body.innerText || '').trim().length;
                        const ready = hasArchived || hasMedia || textLen > 40
                            || document.body.children.length > 3;
                        return { ready, size };
                    }"""
                )
            except Exception:
                logger.debug("Error while checking Archive.org playback readiness", exc_info=True)
                info = {"ready": False, "size": 0}

            if info.get("ready"):
                size = int(info.get("size") or 0)
                # Two consecutive similar snapshots (~100ms apart) ⇒ content settled.
                if previous_size >= 0 and abs(size - previous_size) <= max(256, previous_size // 100):
                    stable_checks += 1
                    if stable_checks >= 2:
                        return
                else:
                    stable_checks = 0
                previous_size = size
            else:
                previous_size = -1
                stable_checks = 0

            await asyncio.sleep(0.1)

        logger.debug("Archive.org playback frame did not report ready before timeout; continuing.")

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
        while time.monotonic() < deadline:
            if served:
                # The player already fetched the real video: nothing more to wait for
                return fallback
            frames = []
            if preferred is not None:
                frames.append(preferred)
            for f in page.frames:
                if f not in frames:
                    frames.append(f)
            for frame in frames:
                if await self._frame_has_primary_video(frame):
                    return frame
            await asyncio.sleep(0.25)
        logger.debug("Archive.org primary video did not appear before timeout; continuing.")
        return fallback

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
                        frame = await self._wait_for_primary_video(page, preferred=frame)
                    await _stash_media_in_frame(frame)
                    if wants_video:
                        await self._inline_served_video(frame, page)
                    return frame

            # Rewritten snapshot without playback iframe (content already on the top frame).
            target: Frame = page.main_frame
            if wants_video:
                await self._wait_playback_frame_ready(target)
                target = await self._wait_for_primary_video(page, preferred=target)
                await _stash_media_in_frame(target)
                await self._inline_served_video(target, page)
                return target
            return page

        except PlaywrightError:
            raise RetrievalFailed("Archive.org playback iframe not loaded successfully.")
