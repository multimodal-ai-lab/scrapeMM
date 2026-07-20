import asyncio
import logging
import time
from typing import Optional

from playwright.async_api import TimeoutError, Page, Frame

from scrapemm.integrations.headed_browser import HeadedBrowser, ContentTarget
from scrapemm.integrations.perma_cc import _inline_media_in_frame

logger = logging.getLogger("scrapeMM")

_PLAYBACK_IFRAME = "#playback iframe, iframe#playback"


class ArchiveOrg(HeadedBrowser):
    """Integration for retrieving content from archive.org (Internet Archive)."""
    name = "Internet Archive"
    domains = ["archive.org"]

    async def _settle_after_goto(self, page: Page) -> None:
        """Wait only until the Wayback playback iframe appears (or give up quickly)."""
        try:
            await page.wait_for_selector(_PLAYBACK_IFRAME, timeout=8000)
        except TimeoutError:
            # Rewritten pages without a playback iframe — proceed immediately.
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

    async def _extract_content(self, page: Page) -> Optional[ContentTarget]:
        # Selector was already awaited in _settle_after_goto — no second long wait.
        playback_iframe = await page.query_selector(_PLAYBACK_IFRAME)
        if playback_iframe:
            frame = await playback_iframe.content_frame()
            if frame:
                await self._wait_playback_frame_ready(frame)
                await _inline_media_in_frame(frame)
                return frame

        # Rewritten snapshot without playback iframe
        return page
