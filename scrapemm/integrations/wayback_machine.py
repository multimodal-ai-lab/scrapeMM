import logging
import re
import time
from typing import Self
import aiohttp
from ezmm import MultimodalSequence

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

from attr import dataclass

from scrapemm.integrations.base import RetrievalIntegration
from scrapemm.util import to_multimodal_sequence

logger = logging.getLogger("scrapeMM")


WAYBACK_URL_REGEX = re.compile(
    r"^(https?://web\.archive\.org/web/)(\d{4,14})([a-z_]{2,3})?/(.*)$"
)


@dataclass
class WaybackURL:
    wayback_base: str
    timestamp: str
    archived_url: str
    modifier: str = ""

    @classmethod
    def from_url(cls, url: str) -> Self:
        match = WAYBACK_URL_REGEX.match(url)
        if not match:
            raise ValueError(f"Invalid Wayback URL: {url}")

        return cls(
            wayback_base=match.group(1),
            timestamp=match.group(2),
            modifier=match.group(3) if match.group(3) else "",
            archived_url=match.group(4),
        )

    def __str__(self) -> str:
        return f"{self.wayback_base}{self.timestamp}{self.modifier}/{self.archived_url}"


class WaybackMachine(RetrievalIntegration):
    """Integration for the Wayback Machine."""

    name = "archive.org"
    domains = ["archive.org"]

    async def _connect(self) -> None:
        self.connected = True

    async def _get(self, url: str, **kwargs) -> MultimodalSequence | None:
        wayback_url = WaybackURL.from_url(url)

        if not wayback_url:
            logger.warning(f"Could not parse Wayback Machine URL: {url}")
            return None

        wayback_url = self._add_if_modifier(wayback_url)        
        wayback_url_str = str(wayback_url)

        async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context()
                page = await context.new_page()

                try:
                    await page.goto(wayback_url_str, timeout=60000)
                    print(1)
                    await page.wait_for_load_state("networkidle", timeout=60000)
                    print(2)
                    html = await page.content()

                    if html:
                        print(3)
                        async with aiohttp.ClientSession() as session:
                            return await to_multimodal_sequence(
                                html, remove_urls=False, session=session, url=wayback_url_str
                            )                    

                except PlaywrightTimeoutError:
                    logger.warning(f"Timeout while loading Wayback URL: {wayback_url_str}")
                except PlaywrightError as e:
                    logger.warning(f"Playwright error for URL '{wayback_url_str}': {e}")
                finally:
                    await browser.close()

    def _add_if_modifier(self, wayback_url: WaybackURL) -> WaybackURL:
        """
        Appends the 'if_' modifier to the Wayback URL if not already present.
        This modifier ensures that the archived content is served without
        any additional overlays but hyperlinks still point to the Wayback Machine itself.
        """
        if wayback_url.modifier == "if_":
            return wayback_url

        wayback_url.modifier = "if_"
        return wayback_url

if __name__ == "__main__":
    import asyncio

    async def main() -> None:
        wayback_integration = WaybackMachine()
        await wayback_integration._connect()
        url = "https://web.archive.org/web/20210604181412/https://www.tiktok.com/@realstewpeters/video/6969789589590379781?is_copy_url=1"
        mms = await wayback_integration.get(url)
        print(mms)

    asyncio.run(main())