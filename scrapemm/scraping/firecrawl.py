import logging
from typing import Optional

import aiohttp
import requests
from ezmm import MultimodalSequence

from scrapemm.common import is_no_bot_site, get_config_var, update_config
from scrapemm.scraping.util import find_firecrawl, to_multimodal_sequence, firecrawl_is_running

logger = logging.getLogger("scrapeMM")

FIRECRAWL_URLS = [
    get_config_var("firecrawl_url"),
    "http://localhost:3002",
    "http://firecrawl:3002",
    "http://0.0.0.0:3002",
]


class Firecrawl:
    """Takes any URL and tries to scrape its contents. If the URL belongs to a platform
    requiring an API and the API integration is implemented (e.g. X, Reddit etc.), the
    respective API will be used instead of direct HTTP requests."""

    firecrawl_url: str

    def __init__(self):
        self.locate_firecrawl()
        self.n_scrapes = 0

    def locate_firecrawl(self):
        """Scans a list of URLs (included the user-specified one) to find a
        running Firecrawl instance."""
        firecrawl_url = find_firecrawl(FIRECRAWL_URLS)
        while not firecrawl_url:
            firecrawl_url = input(f"❌ Unable to locate Firecrawl! It is not running "
                                  f"at: {get_config_var('firecrawl_url')}\n"
                                  f"Please enter the URL of your Firecrawl instance: ")
            if firecrawl_url:
                firecrawl_url = firecrawl_url.strip()
                update_config(firecrawl_url=firecrawl_url)

            if not firecrawl_is_running(firecrawl_url):
                firecrawl_url = None

        self.firecrawl_url = firecrawl_url
        logger.info(f"✅ Detected Firecrawl running at {self.firecrawl_url}.")

    async def scrape(self, url: str,
                     remove_urls: bool,
                     session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
        """Downloads the contents of the specified webpages dynamically or statically.
        Resolves up to MAX_MEDIA_PER_PAGE image URLs and replaces them with their
        respective reference."""

        # Skip URLs from domains that are not supported
        if is_no_bot_site(url):
            return None

        html = await self._call_firecrawl(url, session)
        if html:
            return await to_multimodal_sequence(html, remove_urls=remove_urls, session=session)

    async def _call_firecrawl(self, url: str, session: aiohttp.ClientSession,
                              format: str = "html", timeout: int = 30_000) -> Optional[str]:
        """Scrapes the given URL using Firecrawl. Returns a Markdown-formatted string
        of the webpage's contents."""
        assert self.firecrawl_url is not None

        headers = {
            'Content-Type': 'application/json',
        }
        json_data = {
            "url": url,
            "formats": [format],
            "onlyMainContent": False,
            "removeBase64Images": False,
            # "includeTags": [
            #     # Text
            #     "p", "h1", "h2", "h3", "h4", "h5", "h6", "span", "a", "div",
            #     "li", "blockquote", "figcaption", "article", "header", "section", "ul", "ol",
            #     "pre", "code", "table", "tbody", "tr", "td", "th", "thead",
            #     # Media
            #     "img", "picture", "video", "audio", "source", "iframe", "embed", "object",
            # ],
            "excludeTags": ["script", "style", "noscript", "footer", "aside"],
            "timeout": timeout,  # Max. duration in ms that the scraper will wait for the page to respond
        }

        try:
            async with session.post(self.firecrawl_url + "/v1/scrape",
                                    json=json_data,
                                    headers=headers,
                                    timeout=10 * 60) as response:  # Firecrawl scrapes usually take 2 to 4s, but a 1700-page PDF takes 5 min

                if response.status != 200:
                    logger.warning(
                        f"Failed to scrape {url}\nStatus code: {response.status} - Reason: {response.reason}")
                    match response.status:
                        case 402:
                            logger.debug(f"Error 402: Access denied.")
                        case 403:
                            logger.debug(f"Error 403: Forbidden.")
                        case 408:
                            logger.warning(f"Error 408: Timeout! Firecrawl overloaded or Webpage did not respond.")
                        case 409:
                            logger.debug(f"Error 409: Access denied.")
                        case 500:
                            logger.debug(f"Error 500: Server error.")
                        case _:
                            logger.debug(f"Error {response.status}: {response.reason}.")
                    logger.debug("Skipping that URL.")
                    return None

                json = await response.json()
                success = json["success"]
                if success and "data" in json:
                    data = json["data"]
                    text = data.get(format)
                    return text
                else:
                    logger.warning(f"Unable to read {url}. No usable data in response. Skipping it.")
                    logger.debug(str(json))
                    return None

        except (requests.exceptions.RetryError, requests.exceptions.ConnectionError):
            logger.error(f"Firecrawl is not running!")
            return None
        except requests.exceptions.Timeout:
            error_message = "Firecrawl failed to respond in time! This can be due to server overload."
            logger.warning(f"{error_message}\nSkipping the URL {url}.")
            return None
        except Exception as e:
            error_message = f"Exception: {repr(e)}"
            logger.warning(f"{error_message}\nUnable to scrape {url} with Firecrawl. Skipping...")
            return None


firecrawl = Firecrawl()
