import logging

from playwright.async_api import Page

from scrapemm.integrations.headed_browser import HeadedBrowser

logger = logging.getLogger("scrapeMM")


class MediaVault(HeadedBrowser):
    name = "MediaVault"
    domains = ["mvau.lt"]

    async def _extract_content(self, page: Page) -> str | None:
        return await page.content()
