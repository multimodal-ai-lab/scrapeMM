import logging
import sqlite3
import time
from dataclasses import replace
from traceback import format_exc
from typing import Collection, Literal, Coroutine, Callable, Optional

import aiohttp
from ezmm import MultimodalSequence
from playwright._impl._errors import TargetClosedError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

from scrapemm import RateLimitError
from scrapemm.common import (ScrapingResponse, ScrapedContent, OutputFormat, OUTPUT_FORMATS,
                             cache, cache_key, blacklist, detect_captcha)
from scrapemm.common.blacklist import captcha_reason
from scrapemm.common.exceptions import RetrievalFailed, UnsupportedDomainError, DiskFull, \
    TargetUnavailableError, QuotaExceededError, AccessBlockedError, CaptchaEncounteredError
from scrapemm.download import download_image, download_video
from scrapemm.download.common import HEADERS
from scrapemm.download.util import looks_like_image_file_url, looks_like_video_file_url, looks_like_hls_url
from scrapemm.integrations import retrieve_via_integration, fire, decodo, get_integrations_for_url, INTEGRATION_NAMES
from scrapemm.util import run_with_semaphore, get_domain, normalize_video, preprocess_url

logger = logging.getLogger("scrapeMM")
METHODS = ["integrations", "firecrawl", "decodo"]
ALL_METHODS = METHODS + INTEGRATION_NAMES

UNSUPPORTED_DOMAINS = []

BEST_METHODS = {
    # Social media platforms:
    "instagram.com": ["integrations"],
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
    "reddit.com": ["integrations", "firecrawl", "decodo"],
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
        output_format: OutputFormat = "multimodal",
        include_media: bool = True,
        max_video_size: int | None = None,
        prioritize: Literal["completeness", "speed"] = "completeness",
        use_cache: bool = True
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
    :param output_format: The format the content is needed in. Retrieval counts as successful
        only if this format could be produced (see ScrapingResponse.success). Available formats:
        - "multimodal" (response.content.multimodal: MultimodalSequence containing the Markdown
          text of the page along with the media downloaded from it)
        - "markdown" (response.content.markdown: string containing the scraped text in Markdown
          format, media referenced by hyperlink, nothing downloaded)
        - "html" (response.content.html: string containing the raw HTML code of the page. Not
          available for sources that have no HTML page, e.g. the X API.)
        The formats preceding the requested one are populated along the way, i.e., the raw HTML
        is kept whenever the used method had access to it. No format is produced beyond the
        requested one.
    :param include_media: Deprecated, use output_format instead. If False, the "multimodal"
        output format is downgraded to "markdown", i.e., no media gets downloaded.
    :param max_video_size: Maximum size of videos to download, in bytes. If None, no limit is applied.
    :param prioritize: Prioritization strategy for retrieval. Available options:
        - "completeness": Higher timeout limits and more retries.
        - "speed": Lower timeout limits and fewer retries.
    :param use_cache: If True, successful retrievals from the last 24 hours (see
        `set_cache_ttl()` to change that duration) are re-used instead of scraping
        the URL again. The cache is in-memory only, i.e., not persisted.
    """
    # Ensure URLs are string or list
    assert isinstance(urls, (str, list)), "'urls' must be a string or a list of strings."

    assert output_format in OUTPUT_FORMATS, \
        f"Unknown output format '{output_format}'. Allowed: {list(OUTPUT_FORMATS)}"

    if not include_media:
        logger.warning("The 'include_media' parameter is deprecated. Use output_format='markdown' instead.")
        if output_format == "multimodal":
            output_format = "markdown"

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
                                  output_format, max_video_size, prioritize, use_cache) for url in
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
        output_format: OutputFormat = "multimodal",
        max_video_size: int | None = None,
        prioritize: Literal["completeness", "speed"] = "completeness",
        use_cache: bool = True
) -> ScrapingResponse:
    logger.debug(f"Retrieving {url}")
    start_time = time.time()

    if get_domain(url) in UNSUPPORTED_DOMAINS:
        return _failure(url, output_format, dict(scrapemm=UnsupportedDomainError("Unsupported domain.")),
                        start_time)

    # Ensure URL is a string and remove unwanted symbols
    url = preprocess_url(url)

    # Refuse blacklisted domains, e.g., domains that are protected by a CAPTCHA
    domain = get_domain(url)
    if reason := blacklist.reason(domain):
        e = UnsupportedDomainError(f"Domain '{domain}' is blacklisted: {reason}\nCall "
                                   f"scrapemm.unblacklist_domain('{domain}') to allow it again.")
        return _failure(url, output_format, dict(scrapemm=e), start_time)

    # Resolve the best methods to try in order
    methods: list[str] = resolve_best_methods(url, methods)

    if len(methods) == 0:
        e = UnsupportedDomainError("scrapeMM does not support that URL at this time.")
        return _failure(url, output_format, dict(scrapeMM=e), start_time)

    # Re-use a recent, successful retrieval of the same URL, if there is any
    key = cache_key(url, output_format, methods)
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            logger.info(f"📎 Serving {url} from cache.")
            return replace(cached, from_cache=True, retrieval_time=time.time() - start_time)

    try:
        # Validate methods
        for method in methods:
            assert method in ALL_METHODS, f"Unknown method '{method}'. Allowed: {ALL_METHODS}"

        # Shortcut to download as medium
        if output_format == "multimodal":
            medium = None
            if looks_like_image_file_url(url):
                medium = await download_image(url, session=session)
            elif looks_like_video_file_url(url) or looks_like_hls_url(url):
                medium = await download_video(url, session=session)
            if medium:
                content = ScrapedContent(multimodal=MultimodalSequence(medium))
                response = ScrapingResponse(url=url, content=content, method="Direct download",
                                            output_format=output_format,
                                            retrieval_time=time.time() - start_time)
                cache.put(key, response)
                return response

        def map_method_to_retrieval_routine(m: str) -> Coroutine:
            if m.lower() == "firecrawl":
                return fire.scrape(url, session=session, output_format=output_format, actions=actions)
            elif m.lower() == "decodo":
                return decodo.scrape(url, session, output_format=output_format,
                                     timeout=15 if prioritize == "speed" else 60,
                                     max_retries=1 if prioritize == "speed" else 5)
            else:
                return retrieve_via_integration(url, integration_name=m, session=session,
                                                max_video_size=max_video_size,
                                                output_format=output_format)

    except Exception as e:
        logger.error(f"Error while preparing retrieval for '{url}'.\n" + format_exc())
        return _failure(url, output_format, dict(scrapemm=e), start_time)

    # Try each method in the specified order until one succeeds
    errors = {}
    partial = None  # Content that was retrieved, but not in the requested format
    logger.debug(f"Trying methods in order: {', '.join(methods)}")
    for method_name in methods:
        logger.debug(f"Now executing {method_name}...")

        content = await _execute(url, map_method_to_retrieval_routine, method_name, session)

        if isinstance(content, Exception):
            errors[method_name] = content
            if isinstance(content, TargetUnavailableError):
                break  # Not worth trying other methods
            continue

        if not content:
            # Methods are expected to raise instead of returning empty-handed
            logger.info(f"Method {method_name} returned no content for url: {url}.")
            errors[method_name] = RetrievalFailed(f"Method {method_name} returned no content.")
            continue

        # Ensure the method returned the actual content and not a CAPTCHA challenge
        if captcha := detect_captcha(content):
            logger.warning(f"🤖 Method {method_name} encountered a {captcha} at {url}.")
            errors[method_name] = CaptchaEncounteredError(
                f"Method {method_name} encountered a {captcha}."
            )
            continue

        if content.get(output_format) is not None:
            logger.info(f"🎉 Successfully retrieved with method: {method_name}")
            if content.multimodal is not None:
                postprocess_media(content.multimodal)
            response = ScrapingResponse(url=url, content=content, method=method_name, errors=errors,
                                        output_format=output_format,
                                        retrieval_time=time.time() - start_time)
            cache.put(key, response)
            return response
        else:
            # The method retrieved something, just not in the requested format (e.g. the X API
            # has no HTML page to offer). Keep it, but continue with the remaining methods.
            logger.info(f"Method {method_name} could not provide the content of {url} as {output_format}.")
            errors[method_name] = RetrievalFailed(
                f"Method {method_name} did not provide the content as {output_format}."
            )
            partial = partial or content

    # All methods failed
    logger.warning(f"All retrieval methods failed for URL: {url}")

    # Exclude CAPTCHA-protected domains from any future retrieval
    for error in errors.values():
        if isinstance(error, CaptchaEncounteredError):
            blacklist.add(domain, captcha_reason(error, url))
            break
    if partial is not None and partial.multimodal is not None:
        postprocess_media(partial.multimodal)
    return _failure(url, output_format, errors, start_time, content=partial)


def _failure(url: str, output_format: OutputFormat, errors: dict[str, Optional[Exception]],
             start_time: float, content: ScrapedContent | None = None) -> ScrapingResponse:
    """Constructs the ScrapingResponse for a URL that could not be retrieved in the
    requested format. Any content that was retrieved nevertheless is kept."""
    return ScrapingResponse(url=url, content=content, errors=errors, output_format=output_format,
                            retrieval_time=time.time() - start_time)


async def _execute(
        url: str, method_to_routine: Callable[[str], Coroutine], method_name: str,
        session: aiohttp.ClientSession, attempts_remaining: int = 1,
) -> ScrapedContent | Exception | None:
    """Executes a retrieval routine and handles exceptions. If an error occurred, returns
    the exception object in place of the result."""
    try:
        routine = method_to_routine(method_name)  # Apply factory to get the actual routine
        return await routine

    except NotImplementedError as e:
        logger.info("Reached a method that is not implemented.", exc_info=True)
        return e

    except sqlite3.OperationalError as e:
        if str(e) == "attempt to write a readonly database":
            logger.error("ezMM database is read-only! Please check the database.")
            raise
        else:
            logger.warning(f"DB error while retrieving with method {method_name}.", exc_info=True)
            return e

    except (TimeoutError, PlaywrightTimeoutError) as e:
        logger.warning(f"Timeout while retrieving with method {method_name}: {e}")
        return e

    except OSError as e:
        if "Disk is full" in str(e):
            logger.critical("❌ Disk is full! Please free up space and try again. Aborting.")
            raise DiskFull()
        return e

    except RetrievalFailed as e:
        logger.debug(f"Retrieval of {url} with method '{method_name}' failed with {e}.")
        return e

    except AccessBlockedError as e:
        logger.debug(f"Method '{method_name}' was prevented from accessing {url}: {e}")
        return e

    except CaptchaEncounteredError as e:
        logger.warning(f"🤖 Method '{method_name}' encountered a CAPTCHA at {url}: {e}")
        return e

    except TargetUnavailableError as e:
        logger.debug(f"Method '{method_name}' cannot reach target {url}: {e}")
        return e

    except RateLimitError as e:
        logger.warning(f"⚠️ Rate limit reached for {method_name}: {e}")
        return e

    except QuotaExceededError as e:
        logger.error(f"❌ Quota exceeded for {method_name}! {e}")
        return e

    except PlaywrightError as e:
        if attempts_remaining > 0 and (
                "ERR_NETWORK_CHANGED" in str(e)
                or "ECONNREFUSED" in str(e)
                or isinstance(e, TargetClosedError)
        ):
            return await _execute(url, method_to_routine, method_name, session, attempts_remaining - 1)
        else:
            return e

    except Exception as e:
        logger.warning(f"Error while retrieving with method {method_name}.", exc_info=True)
        return e


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
