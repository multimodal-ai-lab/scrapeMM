import asyncio
import logging
import time
from typing import Optional

import aiohttp
from playwright.async_api import Page, Error as PlaywrightError

from scrapemm.common import RetrievalFailed
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.server.download.common import HEADERS
from scrapemm.server.integrations.headed_browser import HeadedBrowser, ContentTarget
from scrapemm.server.util import to_scraped_content

logger = logging.getLogger("scrapeMM")

# NCBI's two bot checks: a JS cookie check, and an invisible reCAPTCHA Enterprise page
# that forwards a real browser to the article after scoring it
CHALLENGE_MARKERS = ("Cookies must be enabled", "recaptcha/challengepage")
CHALLENGE_TIMEOUT = 20  # seconds


def _is_challenge(html: str) -> bool:
    return any(marker in html for marker in CHALLENGE_MARKERS)


class NCBI(HeadedBrowser):
    """PubMed Central, PubMed and the rest of NCBI's sites.

    NCBI scores its visitors. Usually a plain HTTP client gets the full page right away
    (while browser-like clients such as Firecrawl get a cookie check), but at times it
    serves an invisible reCAPTCHA instead. A real browser passes that on its own, so the
    shared headed browser takes over whenever the fast path is challenged."""
    name = "NCBI"
    domains = ["nih.gov"]
    waits_for_browser_slot = False  # Tries plain HTTP first

    async def _get(self, url: str, **kwargs) -> ScrapedContent:
        output_format = kwargs.get("output_format", "multimodal")
        # A session of our own: the default headers are part of what gets us through
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    raise RetrievalFailed(f"NCBI answered HTTP {response.status} for {url}.")
                html = await response.text()

            if not _is_challenge(html):
                return await to_scraped_content(
                    html, session=session, output_format=output_format,
                    url=str(response.url), max_video_size=kwargs.get("max_video_size"))

        logger.debug(f"NCBI challenged the plain request for {url}; using the headed browser.")
        return await super()._get(url, **kwargs)

    async def _extract_content(self, page: Page) -> Optional[ContentTarget]:
        """Waits for the browser to pass the challenge and land on the actual page. Polled
        from here, since the challenge navigates away and would take an in-page wait
        down with it."""
        deadline = time.monotonic() + CHALLENGE_TIMEOUT
        while time.monotonic() < deadline:
            try:
                if not _is_challenge(await page.content()):
                    return page
            except PlaywrightError:
                pass  # Mid-navigation
            await asyncio.sleep(0.5)
        raise RetrievalFailed("NCBI's bot check did not let the headed browser through.")
