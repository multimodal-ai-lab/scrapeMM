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


def locate_firecrawl() -> str:
    """Scans a list of URLs (included the user-specified one) to find a
    running Firecrawl instance."""
    firecrawl_url = find_firecrawl(FIRECRAWL_URLS)
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

        if not firecrawl_is_running(firecrawl_url):
            firecrawl_url = None

    logger.info(f"✅ Detected Firecrawl running at {firecrawl_url}.")
    return firecrawl_url


class Firecrawl:
    """Wrapper around the AsyncFirecrawl class to handle pre- and post-processing."""

    firecrawl_url: str

    def __init__(self):
        self.n_scrapes = 0
        self._firecrawl = None

    def connect(self):
        from firecrawl import AsyncFirecrawl
        logging.getLogger("firecrawl").setLevel(logging.WARNING)
        self.firecrawl_url = locate_firecrawl()
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
            self.connect()

        self.n_scrapes += 1
        document = await self._firecrawl.scrape(url,
                                                formats=["html"],
                                                only_main_content=False,
                                                remove_base64_images=False,
                                                exclude_tags=["script", "style", "noscript", "footer", "aside"],
                                                timeout=30_000,
                                                wait_for=1_000,
                                                **kwargs)
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


def find_firecrawl(urls):
    for url in urls:
        if firecrawl_is_running(url):
            return url
    return None


def firecrawl_is_running(url: str) -> bool:
    """Returns True iff Firecrawl is running at the specified URL."""
    if not url:
        return False
    try:
        if not url.startswith("http"):
            url = "https://" + url
        response = requests.get(url, timeout=0.2)
    except (ConnectionError, RetryError, ReadTimeout):
        return False
    return response.status_code == 200
