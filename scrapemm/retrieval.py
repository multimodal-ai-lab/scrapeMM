import logging
from traceback import format_exc

import aiohttp
from ezmm import MultimodalSequence

from scrapemm.integrations import retrieve_via_integration
from scrapemm.scraping.decodo import decodo
from scrapemm.scraping.firecrawl import firecrawl
from scrapemm.util import run_with_semaphore

logger = logging.getLogger("scrapeMM")


async def retrieve(urls: str | list[str], remove_urls: bool = True,
                   show_progress: bool = True,
                   methods: list[str] = ["integrations", "decodo", "firecrawl"]) -> MultimodalSequence | list[MultimodalSequence | None] | None:
    """Main function of this repository. Downloads the contents present at the given URL(s).
    For each URL, returns a MultimodalSequence containing text, images, and videos.
    Returns None if the corresponding URL is not supported or if retrieval failed.

    :param urls: The URL(s) to retrieve.
    :param remove_urls: Whether to remove URLs from hyperlinks contained in the
        retrieved text (and only keep the hypertext).
    :param show_progress: Whether to show a progress bar for batch retrieval.
    :param methods: List of retrieval methods to use in order. Available methods:
        "integrations" (API integrations for Twitter, Instagram, etc.),
        "decodo" (Decodo Web Scraping API),
        "firecrawl" (Firecrawl scraping service).
        Default is ["integrations", "decodo", "firecrawl"].
        You can specify any subset in any order, e.g., ["decodo", "firecrawl"] or ["integrations"].
    TODO: Add ability to suppress progress bar.
    TODO: Add ability to navigate the webpage
    """

    async with aiohttp.ClientSession() as session:
        if isinstance(urls, str):
            return await _retrieve_single(urls, remove_urls, session, methods)

        elif isinstance(urls, list):
            if len(urls) == 0:
                return []
            elif len(urls) == 1:
                return [await _retrieve_single(urls[0], remove_urls, session, methods)]

            # Remove duplicates
            urls_unique = set(urls)

            # Retrieve URLs concurrently
            tasks = [_retrieve_single(url, remove_urls, session, methods) for url in urls_unique]
            results = await run_with_semaphore(tasks, limit=20, show_progress=show_progress,
                                               progress_description="Retrieving URLs...")

            # Reconstruct output list
            results = dict(zip(urls_unique, results))
            return [results[url] for url in urls]

        else:
            raise ValueError("'urls' must be a string or a list of strings.")


async def _retrieve_single(url: str, remove_urls: bool,
                           session: aiohttp.ClientSession,
                           methods: list[str]) -> MultimodalSequence | None:
    try:
        # Ensure URL is a string
        url = str(url)

        # Define available retrieval methods
        method_map = {
            "integrations": lambda: retrieve_via_integration(url, session),
            "decodo": lambda: decodo.scrape(url, remove_urls, session),
            "firecrawl": lambda: firecrawl.scrape(url, remove_urls, session),
        }

        # Try each method in the specified order until one succeeds
        for method_name in methods:
            if method_name not in method_map:
                logger.warning(f"Unknown retrieval method '{method_name}'. Skipping...")
                continue

            logger.debug(f"Trying method: {method_name}")
            result = await method_map[method_name]()

            if result is not None:
                logger.debug(f"Successfully retrieved with method: {method_name}")
                return result

        # All methods failed
        logger.debug(f"All retrieval methods failed for URL: {url}")
        return None

    except Exception as e:
        logger.error(f"Error while retrieving URL '{url}'.\n" + format_exc())
        return None
