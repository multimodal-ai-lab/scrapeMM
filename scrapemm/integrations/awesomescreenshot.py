import logging
from typing import Optional

from playwright.async_api import TimeoutError, Page

from scrapemm.integrations.headed_browser import HeadedBrowser, ContentTarget

logger = logging.getLogger("scrapeMM")


class AwesomeScreenshot(HeadedBrowser):
    name = "AwesomeScreenshot"
    domains = ["awesomescreenshot.com"]

    async def _extract_content(self, page: Page) -> Optional[ContentTarget]:
        """The platform shows either an image or a video. Wait for the matching wrapper."""
        try:
            element = await page.wait_for_selector(
                "div.img-wrapper, div.video-box", timeout=5000
            )
            if element:
                # Prefer an iframe's content frame when present; otherwise the wrapper div.
                if frame := await element.content_frame():
                    return frame
                return element
        except TimeoutError:
            logger.debug("Timed out waiting for AwesomeScreenshot media wrapper.")
        logger.debug("Could not find image or video in AwesomeScreenshot page. "
                     "Perhaps the structure has changed?")
        return page
