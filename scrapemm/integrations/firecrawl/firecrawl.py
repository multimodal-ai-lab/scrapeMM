import asyncio
import logging
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import ClientResponseError
from ezmm import MultimodalSequence
from requests import ConnectionError, ReadTimeout
from requests.exceptions import RetryError

from scrapemm.common import get_config_var, update_config
from scrapemm.download.common import HEADERS
from scrapemm.util import read_urls_from_file, get_domain, to_multimodal_sequence

logger = logging.getLogger("scrapeMM")

FIRECRAWL_URLS = [
    "http://localhost:3002",
    "http://firecrawl:3002",
    "http://0.0.0.0:3002",
]
if config_url := get_config_var("firecrawl_url"):
    FIRECRAWL_URLS = [config_url] + FIRECRAWL_URLS

NO_BOT_DOMAINS_FILE_PATH = Path(__file__).parent / "no_bot_domains.txt"
NO_BOT_DOMAINS = read_urls_from_file(NO_BOT_DOMAINS_FILE_PATH)


async def locate_firecrawl() -> str:
    """Scans a list of URLs (included the user-specified one) to find a
    running Firecrawl instance."""
    firecrawl_url = await find_firecrawl(FIRECRAWL_URLS)
    while not firecrawl_url:
        current_url = get_config_var("firecrawl_url") or "any of " + ", ".join(FIRECRAWL_URLS)
        firecrawl_url = input(f"❌ Unable to locate Firecrawl! It is not running "
                              f"at {current_url}\n"
                              f"Please enter the URL of your Firecrawl instance: ")
        if firecrawl_url:
            # Post-process input
            firecrawl_url = firecrawl_url.strip()
            if not firecrawl_url.startswith("http"):
                firecrawl_url = "https://" + firecrawl_url

            update_config(firecrawl_url=firecrawl_url)

        if not await firecrawl_is_running(firecrawl_url):
            firecrawl_url = None

    return firecrawl_url


class Firecrawl:
    """Wrapper around the AsyncFirecrawl class to handle pre- and post-processing."""

    firecrawl_url: str

    def __init__(self):
        self.n_scrapes = 0
        self._firecrawl = None

    async def connect(self):
        from firecrawl import AsyncFirecrawl
        logging.getLogger("firecrawl").setLevel(logging.WARNING)
        self.firecrawl_url = await locate_firecrawl()
        if self.firecrawl_url:
            logger.info(f"✅ Detected Firecrawl running at {self.firecrawl_url}.")
        self._firecrawl = AsyncFirecrawl(api_url=self.firecrawl_url)

    async def scrape(self,
                     url: str,
                     remove_urls: bool,
                     session: aiohttp.ClientSession,
                     format: str,
                     max_attempts: int = 3,
                     **kwargs) -> Optional[MultimodalSequence | str]:
        if is_no_bot_site(url):
            raise ValueError(f"Firecrawl cannot scrape sites from {get_domain(url)}")

        if not self._firecrawl:
            await self.connect()

        if (await self._is_available(url, session)) == False:
            # Skip unavailable URLs which would otherwise cause Firecrawl to get stuck in an infinite loop.
            return None

        document = None
        for attempt in range(max_attempts):
            try:
                document = await self._firecrawl.scrape(
                    url,
                    formats=["html"],
                    only_main_content=False,
                    remove_base64_images=False,
                    exclude_tags=["script", "style", "noscript", "footer", "aside"],
                    timeout=30_000,
                    wait_for=1_000,
                    store_in_cache=False,
                    **kwargs
                )
                break
            except Exception as e:
                # Ensure firecrawl is still running
                state = await get_firecrawl_state(self.firecrawl_url)
                if state == "unavailable":
                    logger.error(f"❌ Firecrawl stopped running at {self.firecrawl_url}.")
                    raise RuntimeError("Firecrawl stopped running.")
                elif state == "busy":
                    if attempt < max_attempts - 1:
                        logger.warning(f"⚠️ Firecrawl seems busy. Retrying in 10 seconds...")
                        await asyncio.sleep(10)
                    else:
                        raise e
                else:
                    raise e

        self.n_scrapes += 1
        html = document.html

        if html:
            if format == "html":
                return html
            else:
                return await to_multimodal_sequence(html, remove_urls=remove_urls,
                                                    session=session, url=url)
        return None

    async def _is_available(self, url: str, session: aiohttp.ClientSession) -> bool | None:
        """Probe if the URL is reachable. If an HTTP error >= 400 occurs, return False."""
        try:
            async with session.head(url, timeout=2) as response:
                response.raise_for_status()
                return True
        except (ReadTimeout, asyncio.TimeoutError):
            return None  # We don't know yet'
        except ClientResponseError as e:
            logger.debug(f"Firecrawl skipping URL {url} due to unavailability: {e}")
            return False


fire = Firecrawl()


def is_no_bot_site(url: str) -> bool:
    """Checks if the URL belongs to a known unsupported website."""
    domain = get_domain(url)
    return domain is None or domain.endswith(".gov") or domain in NO_BOT_DOMAINS


async def find_firecrawl(urls):
    for url in urls:
        if await firecrawl_is_running(url):
            return url
    return None


async def firecrawl_is_running(url: str) -> bool:
    """Returns True iff Firecrawl can be successfully pinged at the specified URL."""
    return await get_firecrawl_state(url) == "running"


async def get_firecrawl_state(url: str) -> str | None:
    """Returns the state of Firecrawl at the specified URL."""
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        # Retrieve the head of the homepage
        try:
            async with session.head(url, timeout=2) as response:
                if 200 <= response.status < 400:
                    return "running"
        except (ReadTimeout, asyncio.TimeoutError):
            return "busy"
        except (aiohttp.ClientError, ConnectionError, RetryError):
            return "unavailable"
