import logging
from pathlib import Path
from typing import Optional

import aiohttp
import requests
from ezmm import MultimodalSequence
from requests import ConnectionError, ReadTimeout
from requests.exceptions import RetryError

from scrapemm.common import get_config_var, update_config
from scrapemm.util import read_urls_from_file, get_domain, get_domain_root, to_multimodal_sequence

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
                     **kwargs) -> Optional[MultimodalSequence | str]:
        if is_no_bot_site(url):
            raise ValueError(f"Firecrawl cannot scrape sites from {get_domain(url)}")

        if not self._firecrawl:
            await self.connect()

        try:
            document = await self._firecrawl.scrape(
                url,
                formats=["html"],
                only_main_content=False,
                remove_base64_images=False,
                exclude_tags=["script", "style", "noscript", "footer", "aside"],
                timeout=30_000,
                wait_for=1_000,
                **kwargs
            )
        except Exception as e:
            # Ensure firecrawl is still running
            if not await firecrawl_is_running(self.firecrawl_url):
                logger.error(f"❌ Firecrawl stopped running at {self.firecrawl_url}.")
                raise RuntimeError("Firecrawl stopped running.")
            else:
                raise e

        self.n_scrapes += 1
        html = document.html

        if html:
            if format == "html":
                return html
            else:
                domain_root = get_domain_root(url)
                return await to_multimodal_sequence(html, remove_urls=remove_urls,
                                                    session=session, domain_root=domain_root)
        return None


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
    if not url:
        return False
    if not url.startswith("http"):
        url = "https://" + url

    async with aiohttp.ClientSession() as session:
        # Retrieve the head of the homepage
        try:
            async with session.head(url, timeout=1) as response:
                return response.status == 200
        except (aiohttp.ClientError, ConnectionError, ReadTimeout, RetryError, TimeoutError):
            return False
