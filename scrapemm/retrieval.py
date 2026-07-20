import logging
import sqlite3
import time
from traceback import format_exc
from typing import Collection, Literal

import aiohttp
from ezmm import MultimodalSequence
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from scrapemm.common import ScrapingResponse
from scrapemm.common.exceptions import RetrievalFailed, IPBannedError, UnsupportedDomainError, DiskFull
from scrapemm.download import download_image, download_video
from scrapemm.download.common import HEADERS
from scrapemm.download.util import looks_like_image_file_url, looks_like_video_file_url, looks_like_hls_url
from scrapemm.integrations import retrieve_via_integration, fire, decodo, get_integrations_for_url, INTEGRATION_NAMES
from scrapemm.util import run_with_semaphore, get_domain, normalize_video

logger = logging.getLogger("scrapeMM")
METHODS = ["integrations", "firecrawl", "decodo"]
ALL_METHODS = METHODS + INTEGRATION_NAMES

UNSUPPORTED_DOMAINS = []

BEST_METHODS = {
    # Social media platforms:
    "instagram.com": ["integrations", "decodo"],
    "facebook.com": ["integrations"],
    "fb.watch": ["integrations"],
    "x.com": ["integrations"],
    "twitter.com": ["integrations"],
    "t.co": ["integrations"],
    "t.me": ["integrations"],
    "tiktok.com": ["integrations"],
    "telegram.me": ["integrations"],
    "bsky.app": ["integrations"],
    "truthsocial.com": ["firecrawl"],
    "reddit.com": ["integrations"],
    "youtube.com": ["integrations"],
    "youtu.be": ["integrations"],
    # Archiving services:
    "archive.today": ["integrations"],
    "archive.is": ["integrations"],
    "archive.ph": ["integrations"],
    "archive.vn": ["integrations"],
    "archive.li": ["integrations"],
    "archive.fo": ["integrations"],
    "archive.md": ["integrations"],
    "perma.cc": ["integrations"],
    "archive.org": ["integrations"],
    "mvau.lt": ["integrations"],
    "awesomescreenshot.com": ["integrations"],
    # Miscellaneous:
    "washingtonpost.com": ["decodo"],
    "verafiles.org": ["decodo", "firecrawl"],
    "youturn.in": ["decodo"],
}


async def retrieve(
        urls: str | Collection[str],
        show_progress: bool = True,
        actions: list[dict] | None = None,
        methods: Literal["auto"] | list[str] | list[Literal["auto"] | list[str]] = "auto",
        format: Literal["multimodal_sequence", "html"] = "multimodal_sequence",
        include_media: bool = True,
        max_video_size: int | None = None,
        prioritize: Literal["completeness", "speed"] = "completeness"
) -> ScrapingResponse | list[ScrapingResponse]:
    """Main function of this repository. Downloads the contents present at the given URL(s).
    For each URL, returns a ScrapingResponse containing the retrieved content, error, and method.

    :param urls: The URL(s) to retrieve.
    :param show_progress: Whether to show a progress bar while retrieving URLs.
    :param actions: A list of actions to perform with Firecrawl on the webpage before scraping.
        The actions will be ignored if an API integration (e.g., TikTok) is used to retrieve the content.
        As of Nov 2025, self-hosted Firecrawl instances do not support actions.
    :param methods: List of retrieval methods to use in order. Available methods:
        - "integrations" (API integrations for Twitter, Instagram, etc.)
        - "firecrawl" (Firecrawl scraping service)
        - "decodo" (Decodo Web Scraping API)
        You can specify any subset in any order, e.g., ["decodo", "firecrawl"] or ["integrations"]. If provided
        a list of strings, that order of methods will be applied to all submitted URLs. In contrast, if provided
        a list of lists, each list will be applied to the corresponding URL in the batch. If provided "auto",
        will determine the best method based on the URL's domain. If "auto", will use the default order.
    :param format: The format of the output. Available formats:
        - "multimodal_sequence" (MultimodalSequence containing parsed and downloaded media from the page)
        - "html" (string containing the raw HTML code of the page, not compatible with 'integrations' method)
    :param include_media: If True (default), download and embed images/videos. If False, return text only
        (media elements are omitted). This parameter is ignored for HTML format.
    :param max_video_size: Maximum size of videos to download, in bytes. If None, no limit is applied.
    :param prioritize: Prioritization strategy for retrieval. Available options:
        - "completeness": Higher timeout limits and more retries.
        - "speed": Lower timeout limits and fewer retries.
    """
    # Ensure URLs are string or list
    assert isinstance(urls, (str, list)), "'urls' must be a string or a list of strings."

    single_url = isinstance(urls, str)
    urls_to_retrieve: list[str] = [urls] if single_url else urls

    if len(urls_to_retrieve) == 0:
        return []

    if actions:
        raise NotImplementedError("Actions are not supported yet.")

    if methods == "auto":
        methods = len(urls_to_retrieve) * ["auto"]
    elif methods is None:
        methods = len(urls_to_retrieve) * [METHODS.copy()]  # Use copy to avoid modifying the original list
    elif isinstance(methods, list):
        assert len(methods) >= 1, "'methods' cannot be an empty list."

    # Unfold methods list to build per-URL method dict according to the provided 'methods'
    if isinstance(methods[0], str) and methods[0] != "auto":
        # methods: list[str] → apply same order to all URLs
        url_to_methods = {url: methods[:] for url in urls_to_retrieve}
    elif isinstance(methods[0], list) or methods[0] == "auto":
        # methods: list[list[str] | "auto"] → each inner list corresponds to the URL at the same index
        url_to_methods = dict(zip(urls_to_retrieve, methods))
    else:
        raise AssertionError("'methods' must be either None, 'auto', list[str] or a list[list[str] | 'auto'].")

    urls_unique = set(urls_to_retrieve)

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        # Retrieve URLs concurrently
        tasks = [_retrieve_single(url, session, url_to_methods[url], actions,
                                  format, include_media, max_video_size, prioritize) for url in
                 urls_unique]
        results = await run_with_semaphore(tasks, limit=40, show_progress=show_progress and len(urls_unique) > 1,
                                           progress_description="Retrieving URLs...")

        # Reconstruct output list
        results = dict(zip(urls_unique, results))
        if single_url:
            return results[urls]
        else:
            return [results[url] for url in urls_to_retrieve]


async def _retrieve_single(
        url: str,
        session: aiohttp.ClientSession,
        methods: Literal["auto"] | list[str] = "auto",
        actions: list[dict] | None = None,
        format: Literal["multimodal_sequence", "html"] = "multimodal_sequence",
        include_media: bool = True,
        max_video_size: int | None = None,
        prioritize: Literal["completeness", "speed"] = "completeness"
) -> ScrapingResponse:
    logger.debug(f"Retrieving {url}")
    start_time = time.time()

    if get_domain(url) in UNSUPPORTED_DOMAINS:
        return ScrapingResponse(url=url, content=None,
                                errors=dict(scrapemm=UnsupportedDomainError("Unsupported domain.")),
                                retrieval_time=time.time() - start_time)

    # Ensure URL is a string
    url = str(url)

    # Resolve the best methods to try in order
    methods: list[str] = resolve_best_methods(url, methods)

    if len(methods) == 0:
        raise AssertionError("No retrieval methods were resolved for the given URL.")

    try:
        # Validate methods
        for method in methods:
            assert method in ALL_METHODS, f"Unknown method '{method}'. Allowed: {ALL_METHODS}"

            # Ensure compatibility with methods (local list only — never mutate shared globals)
            if format == "html" and method not in ["decodo", "firecrawl"]:
                raise AssertionError(
                    "'html' format is only compatible with 'firecrawl' and 'decodo' method. "
                    "Please choose a different format or use a different method."
                )

        # Shortcut to download as medium
        if format != "html" and include_media:
            medium = None
            if looks_like_image_file_url(url):
                medium = await download_image(url, session=session)
            elif looks_like_video_file_url(url) or looks_like_hls_url(url):
                medium = await download_video(url, session=session)
            if medium:
                return ScrapingResponse(url=url, content=MultimodalSequence(medium), method="Direct download",
                                        retrieval_time=time.time() - start_time)

        # Map method names to their corresponding retrieval functions
        method_retrieval_tasks = []
        for method in methods:
            if method == "firecrawl":
                method_retrieval_tasks.append(fire.scrape(url, session=session, format=format, actions=actions,
                                                include_media=include_media))
            elif method == "decodo":
                method_retrieval_tasks.append(decodo.scrape(url, session, format=format,
                                                  timeout=15 if prioritize == "speed" else 60,
                                                  max_retries=1 if prioritize == "speed" else 5,
                                                  include_media=include_media))
            else:
                method_retrieval_tasks.append(retrieve_via_integration(url, integration_name=method, session=session,
                                                             max_video_size=max_video_size,
                                                             include_media=include_media))

    except Exception as e:
        logger.error(f"Error while preparing retrieval for '{url}'.\n" + format_exc())
        return ScrapingResponse(url=url, content=None, errors=dict(scrapemm=e), retrieval_time=time.time() - start_time)

    # Try each method in the specified order until one succeeds
    result = None
    errors = {}
    logger.debug(f"Trying methods in order: {', '.join(methods)}")
    for method_name, retrieval_task in zip(methods, method_retrieval_tasks):
        logger.debug(f"Trying method: {method_name}")

        try:
            result = await retrieval_task

        except NotImplementedError as e:
            logger.info("Reached a method that is not implemented.", exc_info=True)
            errors[method_name] = f"{type(e).__name__}: {e}"

        except sqlite3.OperationalError as e:
            if str(e) == "attempt to write a readonly database":
                logger.error("ezMM database is read-only! Please check the database.")
                raise
            else:
                logger.warning(f"Error while retrieving with method '{method_name}': {e}")
                errors[method_name] = f"{type(e).__name__}: {e}"

        except (TimeoutError, PlaywrightTimeoutError) as e:
            logger.warning(f"Timeout while retrieving with method '{method_name}': {e}")
            errors[method_name] = f"{type(e).__name__}: {e}"

        except IPBannedError as e:
            logger.info(e)
            errors[method_name] = f"{type(e).__name__}: {e}"

        except OSError as e:
            if "Disk is full" in str(e):
                logger.critical("❌ Disk is full! Please free up space and try again. Aborting.")
                raise DiskFull()

        except Exception as e:
            logger.warning(f"Error while retrieving with method '{method_name}': {e}")
            errors[method_name] = f"{type(e).__name__}: {e}"

        if result is not None:
            logger.debug(f"Successfully retrieved with method: {method_name}")
            if isinstance(result, MultimodalSequence):
                postprocess_media(result)
            return ScrapingResponse(url=url, content=result, method=method_name,
                                    retrieval_time=time.time() - start_time)

        # Method returned None without raising — record an explicit error
        if method_name not in errors:
            errors[method_name] = RetrievalFailed(
                f"Method '{method_name}' returned no content for '{url}'."
            )

    # All methods failed
    logger.warning(f"All retrieval methods failed for URL: {url}")
    return ScrapingResponse(url=url, content=None, errors=errors, retrieval_time=time.time() - start_time)


def resolve_best_methods(url: str, allowed_methods: Literal["auto"] | list[str]) -> list[str]:
    """Returns the best retrieval methods for the given URL."""
    # Initialize methods list
    methods = get_optimal_methods(url) if allowed_methods == "auto" else allowed_methods

    # Resolve 'integrations' method to specific, applicable integrations, maintaining order
    methods_resolved = []
    for method in methods:
        if method == "integrations":
            methods_resolved.extend(get_integrations_for_url(url))
        else:
            methods_resolved.append(method)

    return methods_resolved


def postprocess_media(result: MultimodalSequence):
    """Ensure all media are located in the default ezmm directory (no temp files)
    and transcode all videos into a format suitable for browser playback."""
    for item in result.unique_items():
        item.relocate(move_not_copy=True)
    from scrapemm import ffmpeg_available
    if ffmpeg_available:
        for video in result.videos:
            normalize_video(video)


def get_optimal_methods(url: str) -> list[str]:
    """Returns the best retrieval methods for the given URL."""
    domain = get_domain(url)
    return BEST_METHODS.get(domain, METHODS).copy()
