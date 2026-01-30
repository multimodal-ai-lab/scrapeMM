import aiohttp
from scrapemm.download.common import HEADERS
from scrapemm.integrations.base import RetrievalIntegration
from playwright.async_api import TimeoutError, async_playwright
from playwright_stealth import Stealth
from ezmm import MultimodalSequence
import logging

from scrapemm.util import to_multimodal_sequence

logger = logging.getLogger("scrapeMM")


class ArchiveToday(RetrievalIntegration):
    name = "Archive.today"
    domains = [
        "archive.today",
        "archive.is",
        "archive.ph",
        "archive.vn",
        "archive.li",
        "archive.fo",
        "archive.md",
    ]

    async def _connect(self):
        self.connected = True

    async def _get(self, url: str, **kwargs) -> MultimodalSequence | None:
        archived_content_html = await self.get_record_html(url)
        if archived_content_html is not None:
            async with aiohttp.ClientSession(headers=HEADERS) as session:
                return await to_multimodal_sequence(
                    archived_content_html, remove_urls=False, session=session, url=url
                )
        else:
            raise RuntimeError("Failed to retrieve Archive.today record HTML.")

    async def get_record_html(self, url: str) -> str | None:
        """Retrieve the HTML content of the archived page at the given URL."""
        async with Stealth().use_async(async_playwright()) as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=HEADERS["User-Agent"],
            )
            page = await context.new_page()

            try:
                await page.goto(url, timeout=30000)
                await page.wait_for_load_state("domcontentloaded")
            except TimeoutError as e:
                logger.warning(
                    f"\rUnable to load page at URL '{url}'.\n\tReason: {type(e).__name__} {e}"
                )

            try:
                await page.wait_for_selector("#CONTENT", timeout=5000)
                return await page.inner_html("#CONTENT")
            except TimeoutError as e:
                logger.warning(
                    f"\rUnable to find content on page at URL '{url}'.\n\tReason: {type(e).__name__} {e}"
                )

            finally:
                await browser.close()
