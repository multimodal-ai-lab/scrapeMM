import logging
from typing import Optional

import aiohttp
from ezmm import MultimodalSequence

from scrapemm.integrations import retrieve_via_integration
from scrapemm.scraping.firecrawl import firecrawl
from scrapemm.util import run_with_semaphore

logger = logging.getLogger("scrapeMM")


async def retrieve(urls: str | list[str], remove_urls: bool = True) -> Optional[MultimodalSequence] | list[Optional[MultimodalSequence]]:
    """Main function of this repository. Downloads the contents present at the given URL(s).
    For each URL, returns a MultimodalSequence containing text, images, and videos.
    Returns None if the corresponding URL is not supported or if retrieval failed.

    :param urls: The URL(s) to retrieve.
    :param remove_urls: Whether to remove URLs from hyperlinks contained in the
        retrieved text (and only keep the hypertext).
    TODO: Add ability to suppress progress bar.
    TODO: Add ability to navigate the webpage
    """

    async with aiohttp.ClientSession() as session:
        if isinstance(urls, str):
            return await _retrieve_single(urls, remove_urls, session)

        elif isinstance(urls, list):
            if len(urls) == 1:
                return await _retrieve_single(urls[0], remove_urls, session)

            # Remove duplicates
            urls_unique = set(urls)

            # Retrieve URLs concurrently
            tasks = [_retrieve_single(url, remove_urls, session) for url in urls_unique]
            results = await run_with_semaphore(tasks, limit=20, show_progress=True,
                                               progress_description="Retrieving URLs...")

            # Reconstruct output list
            results = dict(zip(urls_unique, results))
            return [results[url] for url in urls]

        else:
            raise ValueError("'urls' must be a string or a list of strings.")


async def _retrieve_single(url: str, remove_urls: bool,
                           session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
    try:
        # Ensure URL is a string
        url = str(url)

        # First, try to use a matching API, otherwise scrape directly
        return ((await retrieve_via_integration(url, session)) or
                (await firecrawl.scrape(url, remove_urls, session)))

    except Exception as e:
        logger.error(f"Error while retrieving URL '{url}'.\n"
                     f"{type(e).__name__}: {e}")
