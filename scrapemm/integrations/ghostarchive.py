import asyncio
import logging
from typing import Optional

from playwright.async_api import Page

from scrapemm.integrations.headed_browser import HeadedBrowser, ContentTarget

logger = logging.getLogger("scrapeMM")


class Ghostarchive(HeadedBrowser):
    name = "Ghostarchive"
    domains = ["ghostarchive.org"]

    async def _extract_content(self, page: Page) -> Optional[ContentTarget]:
        # Ghostarchive renders archived content with ReplayWeb.page, which loads the
        # archived page into an <iframe class="iframe-main"> nested inside the replay
        # app (itself in an iframe/shadow DOM). Returning that frame ensures its media
        # is fetched from within the replay service-worker scope.
        content_frame = None
        for _ in range(40):  # ReplayWeb.page loads lazily; poll up to ~20s
            for frame in page.frames:
                try:
                    iframe = await frame.query_selector("iframe.iframe-main")
                    if iframe and (cf := await iframe.content_frame()):
                        content_frame = cf
                        break
                except Exception:
                    continue
            if content_frame:
                break
            await asyncio.sleep(0.5)

        if content_frame is None:
            logger.debug("Ghostarchive 'iframe-main' iframe not found (perhaps not ready or site structure "
                         "has changed); falling back to full page.")
            return page

        # The archived page inside the frame also lazy loads: bootstrap scripts and
        # styles (e.g. wombat, DarkReader) inflate the DOM long before the real content
        # appears, so a pure size threshold returns too early. Wait until the archived
        # content is actually present - ReplayWeb.page rewrites archived media/links to a
        # ".../<mod>_/https://..." replay URL, so the presence of such a rewritten
        # element is a reliable signal - and additionally require the DOM to stop growing
        # so lazily inserted media has settled before the frame is returned.
        previous_size, stable_checks = -1, 0
        for _ in range(60):  # up to ~30s
            try:
                has_archived_content = await content_frame.evaluate(
                    """() => !!document.querySelector(
                        'img[src*="_/http"], video[src*="_/http"], source[src*="_/http"], a[href*="_/http"]'
                    )"""
                )
                size = len(await content_frame.content())
            except Exception:
                has_archived_content, size = False, 0
            dom_stable = size > 5000 and abs(size - previous_size) <= max(256, previous_size // 100)
            if has_archived_content and dom_stable:
                stable_checks += 1
                if stable_checks >= 2:  # content present and DOM unchanged for ~1s
                    break
            else:
                stable_checks = 0
            previous_size = size
            await asyncio.sleep(0.5)

        return content_frame
