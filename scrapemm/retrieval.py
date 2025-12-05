import logging
import sqlite3
from traceback import format_exc
from typing import Collection

import aiohttp
from ezmm import MultimodalSequence, download_item

from scrapemm.common import ScrapingResponse
from scrapemm.integrations import retrieve_via_integration, fire, decodo
from scrapemm.util import run_with_semaphore

logger = logging.getLogger("scrapeMM")
METHODS = ["integrations", "firecrawl", "decodo"]


async def retrieve(
        urls: str | Collection[str],
        remove_urls: bool = False,
        show_progress: bool = True,
        actions: list[dict] = [],
        methods: list[str] = [],
        format: str = "multimodal_sequence",
        max_video_size: int | None = None,
) -> ScrapingResponse | list[ScrapingResponse]:
    """Main function of this repository. Downloads the contents present at the given URL(s).
    For each URL, returns a ScrapingResponse containing the retrieved content, error, and method.

    :param urls: The URL(s) to retrieve.
    :param remove_urls: Whether to remove URLs from hyperlinks contained in the
        retrieved text (and only keep the hypertext).
    :param show_progress: Whether to show a progress bar while retrieving URLs.
    :param actions: A list of actions to perform with Firecrawl on the webpage before scraping.
        The actions will be ignored if an API integration (e.g., TikTok) is used to retrieve the content.
        As of Nov 2025, self-hosted Firecrawl instances do not support actions.
    :param show_progress: Whether to show a progress bar for batch retrieval.
    :param methods: List of retrieval methods to use in order. Available methods:
        - "integrations" (API integrations for Twitter, Instagram, etc.)
        - "firecrawl" (Firecrawl scraping service)
        - "decodo" (Decodo Web Scraping API)
        You can specify any subset in any order, e.g., ["decodo", "firecrawl"] or ["integrations"].
    :param format: The format of the output. Available formats:
        - "multimodal_sequence" (MultimodalSequence containing parsed and downloaded media from the page)
        - "html" (string containing the raw HTML code of the page, not compatible with 'integrations' method)
    :param max_video_size: Maximum size of videos to download, in MB. If None, no limit is applied.
    """
    if methods is None:
        methods = METHODS

    assert len(methods) >= 1
    for method in methods:
        assert method in METHODS

    assert isinstance(urls, (str, list)), "'urls' must be a string or a list of strings."

    # Ensure URLs are string or list
    single_url = isinstance(urls, str)
    urls_to_retrieve = [urls] if single_url else urls

    if len(urls_to_retrieve) == 0:
        return []

    if actions:
        raise NotImplementedError("Actions are not supported yet.")

    async with aiohttp.ClientSession() as session:
        # Remove duplicates
        urls_unique = set(urls_to_retrieve)

        # Retrieve URLs concurrently
        tasks = [_retrieve_single(url, remove_urls, session, methods, actions,
                                  format, max_video_size) for url in
                 urls_unique]
        results = await run_with_semaphore(tasks, limit=20, show_progress=show_progress and len(urls_to_retrieve) > 1,
                                           progress_description="Retrieving URLs...")

        # Reconstruct output list
        results = dict(zip(urls_unique, results))
        if single_url:
            return results[urls]
        else:
            return [results[url] for url in urls_to_retrieve]


async def _retrieve_single(
        url: str,
        remove_urls: bool,
        session: aiohttp.ClientSession,
        methods: list[str],
        actions: list[dict] = [],
        format: str = "multimodal_sequence",
        max_video_size: int | None = None,
) -> ScrapingResponse:
    try:
        # Ensure URL is a string
        url = str(url)

        # Ensure compatibility with methods
        if format == "html" and "integrations" in methods:
            methods.remove("integrations")

        # Try to download as medium
        if format != "html":
            if medium := await download_item(url, session=session):
                return ScrapingResponse(url=url, content=MultimodalSequence(medium), method="ezmm")

        # Find available integrations TODO: Propagate name of actual integration to ScrapingResponse
        method_map = {
            "integrations": lambda: retrieve_via_integration(url, session=session, max_video_size=max_video_size),
            "firecrawl": lambda: fire.scrape(url, remove_urls=remove_urls,
                                             session=session, format=format, actions=actions),
            "decodo": lambda: decodo.scrape(url, remove_urls, session, format=format),
        }

        result = None
        errors = {}

    except Exception as e:
        logger.error(f"Error while preparing retrieval for '{url}'.\n" + format_exc())
        return ScrapingResponse(url=url, content=None, errors=dict(all=e))

    # Try each method in the specified order until one succeeds
    for method_name in methods:
        if method_name not in method_map:
            logger.warning(f"Unknown retrieval method '{method_name}'. Skipping...")
            continue

        logger.debug(f"Trying method: {method_name}")

        try:
            result = await method_map[method_name]()

        except NotImplementedError as e:
            logger.debug(e)
            errors[method_name] = e

        except sqlite3.OperationalError as e:
            if str(e) == "attempt to write a readonly database":
                logger.error("ezMM database is read-only! Please check the database.")
                raise
            else:
                logger.warning(f"Error while retrieving with method '{method_name}': {e}")
                errors[method_name] = e
                result = None

        except Exception as e:
            logger.warning(f"Error while retrieving with method '{method_name}': {e}")
            errors[method_name] = e
            result = None

        if result is not None:
            logger.debug(f"Successfully retrieved with method: {method_name}")
            return ScrapingResponse(url=url, content=result, method=method_name)

    # All methods failed
    logger.warning(f"All retrieval methods failed for URL: {url}")
    return ScrapingResponse(url=url, content=None, errors=errors)
