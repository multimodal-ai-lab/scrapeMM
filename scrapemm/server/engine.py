import asyncio
import ctypes
import ipaddress
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, replace
from traceback import format_exc
from typing import Collection, Literal, Coroutine, Callable, Optional
from urllib.parse import urlsplit

import aiohttp
from ezmm import MultimodalSequence, Image
from ezmm.common.registry import item_registry
from playwright._impl._errors import TargetClosedError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

from scrapemm.common import (ScrapingResponse, ScrapedContent, OutputFormat, OUTPUT_FORMATS,
                             RateLimitError)
from scrapemm.common.exceptions import RetrievalFailed, UnsupportedDomainError, \
    DomainBlacklistedError, DiskFull, TargetUnavailableError, QuotaExceededError, \
    AccessBlockedError, CaptchaEncounteredError, PaywallError
from scrapemm.server import challenges, reachability
from scrapemm.server.blacklist import blacklist
from scrapemm.server.cache import cache, cache_key
from scrapemm.server.captcha_detect import detect_captcha
from scrapemm.server.paywall_detect import detect_paywall
from scrapemm.server.config import get_config_var
from scrapemm.server.download import download_image, download_video
from scrapemm.server.download.common import HEADERS
from scrapemm.server.download.util import (looks_like_image_file_url, looks_like_video_file_url,
                                           looks_like_hls_url, looks_like_pdf_url)
from scrapemm.server.integrations import (retrieve_via_integration, fire, decodo, browser,
                                          get_integrations_for_url, INTEGRATION_NAMES,
                                          DOMAIN_TO_INTEGRATION)
from scrapemm.server.integrations.firecrawl.firecrawl import configured_firecrawl_urls
from scrapemm.server.toggles import filter_methods, is_enabled, resolve_alias
from scrapemm.server.util import (run_with_semaphore, get_domain, normalize_video, preprocess_url,
                                  to_scraped_content)

logger = logging.getLogger("scrapeMM")

# Hedging duplicates work to save latency, so it stays opt-in: None disables it.
DEFAULT_HEDGING_DELAY = None

# The Browser method -- the shared headed browser -- comes before the scraping services:
# on open-web pages it succeeded more often than Firecrawl and as often as Decodo, at a
# fraction of Decodo's time, and it costs nothing. Decodo is paid, so it comes last.
BROWSER = "browser"
METHODS = ["integrations", BROWSER, "firecrawl", "decodo"]
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
        use_cache: bool = True,
        hedging_delay: float | None = None
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
        - "browser" (scrapeMM's own browser, a real Chromium on the server)
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
    :param hedging_delay: Seconds of head start each retrieval method gets before the
        next one is launched alongside it instead of after it. The first method to
        succeed wins and the others are cancelled, so a merely slow method no longer
        makes every later method wait for its full timeout budget. Costs duplicated
        work (and, for paid methods, duplicated requests), hence disabled by default.
        Pass a number to enable it for this call, or set it process-wide via
        `update_config(hedging_delay=5)`. None or 0 runs the methods one after another.
    """
    # Ensure URLs are string or list
    assert isinstance(urls, (str, list)), "'urls' must be a string or a list of strings."

    assert output_format in OUTPUT_FORMATS, \
        f"Unknown output format '{output_format}'. Allowed: {list(OUTPUT_FORMATS)}"

    if hedging_delay is None:
        hedging_delay = get_config_var("hedging_delay", DEFAULT_HEDGING_DELAY)

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
                                  output_format, max_video_size, prioritize, use_cache,
                                  hedging_delay) for url in
                 urls_unique]
        results = await run_with_semaphore(tasks, limit=40, show_progress=show_progress and len(urls_unique) > 1,
                                           progress_description="Retrieving URLs...")
        _release_media_memory()

        # Reconstruct output list
        results = dict(zip(urls_unique, results))
        if single_url:
            return results[urls]
        else:
            return [results[url] for url in urls_to_retrieve]


DEFAULT_MAX_CONCURRENCY = 40

_semaphore: Optional[asyncio.Semaphore] = None
_semaphore_limit: Optional[int] = None


def _concurrency_gate() -> asyncio.Semaphore:
    """The server's global limit on URLs in flight. `retrieve()` applies its own limit
    per batch; the API streams URLs individually, so the limit has to live here instead
    -- otherwise ten concurrent API calls of fifty URLs each would launch five hundred
    retrievals at once."""
    global _semaphore, _semaphore_limit
    limit = int(get_config_var("max_concurrency", DEFAULT_MAX_CONCURRENCY))
    if _semaphore is None or _semaphore_limit != limit:
        _semaphore = asyncio.Semaphore(limit)
        _semaphore_limit = limit
    return _semaphore


async def retrieve_one(
        url: str,
        session: aiohttp.ClientSession,
        methods: Literal["auto"] | list[str] | None = "auto",
        actions: list[dict] | None = None,
        output_format: OutputFormat = "multimodal",
        max_video_size: int | None = None,
        prioritize: Literal["completeness", "speed"] = "completeness",
        use_cache: bool = True,
        hedging_delay: float | None = None,
) -> ScrapingResponse:
    """Retrieves a single URL, subject to the server's concurrency limit. This is what
    the API streams over: it needs results one at a time, as they finish, rather than
    the whole batch at once."""
    if methods is None:
        methods = METHODS.copy()
    if hedging_delay is None:
        hedging_delay = get_config_var("hedging_delay", DEFAULT_HEDGING_DELAY)

    # Identical requests in flight at the same time -- from concurrent jobs, typically --
    # share one retrieval instead of each scraping the same page. The cache only helps
    # once the first of them has finished.
    key = (preprocess_url(url), output_format, json.dumps(methods, default=str),
           json.dumps(actions, sort_keys=True, default=str), max_video_size, prioritize,
           use_cache, hedging_delay)
    shared = _in_flight.get(key)
    joined = shared is not None
    if shared is None:
        shared = _InFlight(asyncio.create_task(_gated_retrieve(
            url, session, methods, actions, output_format, max_video_size, prioritize,
            use_cache, hedging_delay)))
        _in_flight[key] = shared
        shared.task.add_done_callback(lambda _, k=key, s=shared: _forget(k, s))
    else:
        logger.info(f"🔗 Joining the retrieval of {url} that is already under way.")

    shared.waiters += 1
    try:
        # Shielded: one caller going away must not cancel the others' result
        response = await asyncio.shield(shared.task)
    except asyncio.CancelledError:
        if not shared.task.done():
            shared.waiters -= 1
            if shared.waiters == 0:
                shared.task.cancel()  # Nobody is left waiting for it
        raise
    shared.waiters -= 1

    if joined and response.success:
        # Answered without a scrape of its own, which is what a cache hit means
        response = replace(response, from_cache=True)
    # Report under the URL as requested, not as preprocessed (e.g. percent-decoded):
    # the client maps results back onto its request by URL
    return replace(response, url=url)


@dataclass
class _InFlight:
    task: asyncio.Task
    waiters: int = 0


_in_flight: dict[tuple, _InFlight] = {}


def _forget(key: tuple, shared: _InFlight) -> None:
    # Only if it is still this retrieval: a new one for the same key may have started
    if _in_flight.get(key) is shared:
        del _in_flight[key]


async def _gated_retrieve(url, session, methods, actions, output_format, max_video_size,
                          prioritize, use_cache, hedging_delay) -> ScrapingResponse:
    # Its own HTTP session rather than the first caller's: that one closes when its job
    # ends or its client disconnects, while others may still be waiting on this result
    global _active
    async with _concurrency_gate(), aiohttp.ClientSession(headers=HEADERS) as own:
        _active += 1
        try:
            return await _retrieve_single(url, own, methods, actions, output_format,
                                          max_video_size, prioritize, use_cache, hedging_delay)
        finally:
            _active -= 1
            _release_media_memory()


_active = 0  # Retrievals under way, see `_gated_retrieve()`


def _release_media_memory() -> None:
    """Frees the decoded pixels of every image retrieved so far.

    An ezMM Image made from bytes keeps its decoded pixels (up to 2048 x 2048 x 3 bytes,
    12 MB) for as long as the object lives, and ezMM's registry keeps every item it ever
    made. One batch of 40 URLs thereby held 1,350 images and 4 GB of pixels, and the
    process 10 GB, for good. The file on disk has it all: `Image.image` loads the pixels
    again should anybody ask. Once nothing is running, the freed memory is handed back
    to the OS too, which glibc does not do by itself for memory freed in fragments."""
    for item in list(item_registry.cache.values()):
        if isinstance(item, Image) and item._image is not None:
            item._image = None
    if _active == 0 and _libc is not None:
        _libc.malloc_trim(0)


try:
    _libc = ctypes.CDLL("libc.so.6")  # glibc, i.e. the server's Linux; None elsewhere
except OSError:
    _libc = None


async def _retrieve_single(
        url: str,
        session: aiohttp.ClientSession,
        methods: Literal["auto"] | list[str] = "auto",
        actions: list[dict] | None = None,
        output_format: OutputFormat = "multimodal",
        max_video_size: int | None = None,
        prioritize: Literal["completeness", "speed"] = "completeness",
        use_cache: bool = True,
        hedging_delay: float | None = None
) -> ScrapingResponse:
    logger.debug(f"Retrieving {url}")
    start_time = time.time()
    auto = methods == "auto"  # Only then may fallbacks beyond the requested methods run

    if get_domain(url) in UNSUPPORTED_DOMAINS:
        return _failure(url, output_format, dict(scrapemm=UnsupportedDomainError("Unsupported domain.")),
                        start_time)

    # Ensure URL is a string and remove unwanted symbols
    url = preprocess_url(url)

    # Refuse blacklisted domains, e.g., domains that are protected by a CAPTCHA
    domain = get_domain(url)
    if reason := blacklist.reason(domain):
        e = DomainBlacklistedError(
            f"Domain '{domain}' is blacklisted: {reason}\nRemove it under Settings in "
            f"the server's web UI to allow it again.")
        return _failure(url, output_format, dict(scrapemm=e), start_time)

    # Resolve the best methods to try in order. Both lists are kept: the disabled ones
    # explain an empty result better than their absence does.
    applicable: list[str] = applicable_methods(url, methods)
    methods: list[str] = filter_methods(applicable)

    if len(methods) == 0:
        # Distinguish "nothing here can do this" from "you switched off the things that
        # could". The second is a setting somebody can undo, and saying so saves the
        # confusion of a domain that worked yesterday reporting itself unsupported.
        turned_off = [m for m in applicable if not is_enabled(m)]
        e = UnsupportedDomainError(
            f"Every retrieval method for this URL is disabled "
            f"({', '.join(turned_off)}). Re-enable one on the dashboard."
            if turned_off else
            "scrapeMM does not support that URL at this time.")
        return _failure(url, output_format, dict(scrapeMM=e), start_time)

    # Re-use a recent, successful retrieval of the same URL, if there is any
    key = cache_key(url, output_format, methods, max_video_size)
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            logger.info(f"📎 Serving {url} from cache.")
            return replace(cached, from_cache=True, retrieval_time=time.time() - start_time)

    # A domain behind an open CAPTCHA challenge would only gate this URL too: queue it
    # with the challenge, to be retrieved once somebody solves it
    if challenges.store.holds(domain):
        e = CaptchaEncounteredError(
            f"{domain} is behind a CAPTCHA that is waiting for a human. The URL is queued: "
            f"solve the challenge on the CAPTCHA page of the web UI, then request it again "
            f"to get the result.")
        errors = dict(scrapemm=e)
        # An archived copy beats waiting for a human, who may never come
        if auto and (archived := await _archive_fallback(url, domain, session, errors,
                                                         output_format, max_video_size)):
            return await _success(url, key, ARCHIVE_FALLBACK, archived, errors,
                                  output_format, start_time)
        challenge = challenges.store.get(domain)
        challenges.store.record(domain, url, challenge.captcha if challenge else "CAPTCHA",
                                output_format, methods, max_video_size)
        return _failure(url, output_format, errors, start_time)

    # A host that is down fails every method only after its full timeout, minutes in
    # all: find out cheaply first. Integrations' domains are up by definition.
    unreachable: Optional[str] = None  # Why the host cannot be reached from here
    skipped: list[str] = []
    if domain not in DOMAIN_TO_INTEGRATION:
        verdict, reason = await reachability.check(url)
        if verdict == "dead":
            errors = dict(scrapemm=TargetUnavailableError(reason))
            return await _last_resort(url, domain, key, session, errors, output_format,
                                      max_video_size, start_time, auto)
        if verdict == "unreachable":
            # Only from this server, maybe (e.g. its IP is blocked): services that fetch
            # from elsewhere still get their chance, the local methods are skipped
            local = _local_methods()
            skipped = [m for m in methods if m in local]
            methods = [m for m in methods if m not in local]
            unreachable = reason
            if not methods:
                errors = dict(scrapemm=TargetUnavailableError(
                    f"{reason} No enabled method fetches from elsewhere."))
                return await _last_resort(url, domain, key, session, errors, output_format,
                                          max_video_size, start_time, auto)
            logger.info(f"Skipping {', '.join(skipped) or 'no method'} for {url}: the host "
                        f"is unreachable from this server. Trying {', '.join(methods)}.")

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
                medium = await download_video(url, session=session,
                                              max_video_size=max_video_size)
            if medium:
                content = ScrapedContent(multimodal=MultimodalSequence(medium))
                response = ScrapingResponse(url=url, content=content, method="Direct download",
                                            output_format=output_format,
                                            retrieval_time=time.time() - start_time)
                cache.put(key, response)
                return response

        def map_method_to_retrieval_routine(m: str) -> Coroutine:
            if m.lower() == "firecrawl":
                return fire.scrape(url, session=session, output_format=output_format,
                                   actions=actions, max_video_size=max_video_size)
            elif m.lower() == BROWSER:
                return browser._get(url, output_format=output_format,
                                    max_video_size=max_video_size)
            elif m == PLAIN_HTTP:
                return _plain_http(url, session, output_format, max_video_size)
            elif m.lower() == "decodo":
                # Tight when the host is unreachable from here: a proxy getting through
                # does so quickly, and otherwise the host is most likely down
                return decodo.scrape(url, session, output_format=output_format,
                                     timeout=15 if prioritize == "speed" else 30 if unreachable else 60,
                                     max_retries=1 if prioritize == "speed" or unreachable else 5,
                                     max_video_size=max_video_size)
            else:
                return retrieve_via_integration(url, integration_name=m, session=session,
                                                max_video_size=max_video_size,
                                                output_format=output_format)

    except Exception as e:
        logger.error(f"Error while preparing retrieval for '{url}'.\n" + format_exc())
        return _failure(url, output_format, dict(scrapemm=e), start_time)

    # Try the methods until one succeeds
    logger.debug(f"Trying methods in order: {', '.join(methods)}")

    async def evaluate(method_name: str) -> tuple[str, object]:
        """Runs one method and classifies its outcome."""
        logger.debug(f"Now executing {method_name}...")
        content = await _execute(url, map_method_to_retrieval_routine, method_name, session)

        if isinstance(content, Exception):
            return "error", content
        return _classify(url, method_name, content, output_format)

    if hedging_delay and hedging_delay > 0 and len(methods) > 1:
        winner, errors, partial = await _run_methods_hedged(methods, evaluate, hedging_delay,
                                                            output_format)
    else:
        winner, errors, partial = await _run_methods_sequentially(methods, evaluate, output_format)

    if unreachable:
        for m in skipped:
            errors[m] = TargetUnavailableError(f"Skipped: {unreachable}")
        if winner is None and all(isinstance(e, (TimeoutError, asyncio.TimeoutError,
                                                  aiohttp.ClientConnectorError))
                                  for m, e in errors.items() if m not in skipped):
            # Nobody got through from elsewhere either: the host is down. Its other
            # URLs need not try again.
            reachability.mark_dead(url, f"{unreachable} Remote services timed out on it too.")

    if unreachable is None and winner is None and BROWSER not in methods and (content := await _cloudflare_fallback(
            url, domain, session, errors, output_format, max_video_size)):
        winner = (BROWSER, content)

    # Last resort for the open web: the page as a plain request gets it
    # (Not for a page a method found missing: a plain request would find it missing too)
    if (winner is None and auto and domain not in DOMAIN_TO_INTEGRATION and not unreachable
            and not any(isinstance(e, TargetUnavailableError) for e in errors.values())):
        status, result = await evaluate(PLAIN_HTTP)
        if status == "success":
            winner = (PLAIN_HTTP, result)
        elif status == "error":
            errors[PLAIN_HTTP] = result

    if winner is not None:
        return await _success(url, key, *winner, errors, output_format, start_time)

    # All methods failed
    logger.warning(f"All retrieval methods failed for URL: {url}")

    # Nothing live worked: an archived copy is better than nothing
    if auto and (archived := await _archive_fallback(url, domain, session, errors,
                                                     output_format, max_video_size)):
        return await _success(url, key, ARCHIVE_FALLBACK, archived, errors, output_format,
                              start_time)

    # Queue the URL with a CAPTCHA challenge for a human to decide on
    _record_challenge(domain, url, errors, output_format, methods, max_video_size)

    if partial is not None and partial.multimodal is not None:
        await postprocess_media(partial.multimodal)
    return _failure(url, output_format, errors, start_time, content=partial)


def _classify(url: str, method_name: str, content: Optional[ScrapedContent],
              output_format: OutputFormat) -> tuple[str, object]:
    """Judges what a method returned: "success", "partial" (content, but not in the
    requested format) or "error", along with the content or the exception."""
    if not content:
        # Methods are expected to raise instead of returning empty-handed
        logger.info(f"Method {method_name} returned no content for url: {url}.")
        return "error", RetrievalFailed(f"Method {method_name} returned no content.")

    _derive_markdown(content)

    # Ensure the method returned the actual content and not a CAPTCHA challenge
    if captcha := detect_captcha(content):
        logger.warning(f"🤖 Method {method_name} encountered a {captcha} at {url}.")
        return "error", CaptchaEncounteredError(f"Method {method_name} encountered a {captcha}.")

    # ...nor only the teaser of a paywalled article
    if reason := detect_paywall(content):
        logger.info(f"💰 Method {method_name} got only the paywalled teaser of {url}: {reason}.")
        return "error", PaywallError(f"Method {method_name} could not get around the "
                                     f"paywall: {reason}.")

    # ...nor an empty page: it would count as a success and be cached, so that
    # asking again returned nothing too (seen with archive replays that never loaded)
    if _is_empty(content, output_format):
        logger.info(f"Method {method_name} returned an empty page for {url}.")
        return "error", RetrievalFailed(f"Method {method_name} returned an empty page "
                                        f"without any text or media.")

    if content.get(output_format) is not None:
        return "success", content

    # The method retrieved something, just not in the requested format (e.g. the X API
    # has no HTML page to offer). Keep it, but continue with the remaining methods.
    logger.info(f"Method {method_name} could not provide the content of {url} as {output_format}.")
    return "partial", content


def _derive_markdown(content: ScrapedContent) -> None:
    """Fills in the Markdown of content that only came as a multimodal sequence, as the
    integrations (TikTok, X, Telegram, ...) deliver it: the sequence's text with every
    medium referenced by hyperlink to where it came from, as in the Markdown of a web
    page, or by its ezMM reference where there is no web address to link to. So every
    method provides the "markdown" format, not just those that scrape HTML."""
    if content.markdown is not None or content.multimodal is None:
        return
    parts = []
    for element in content.multimodal:
        if isinstance(element, str):
            parts.append(element)
            continue
        source = getattr(element, "source_url", None) or ""
        if not source.startswith(("http://", "https://")):
            parts.append(element.reference)  # E.g. a file:// URI: meaningless elsewhere
        elif isinstance(element, Image):
            parts.append(f"![{element.reference}]({source})")
        else:
            parts.append(f"[{element.kind}: {element.reference}]({source})")
    content.markdown = " ".join(parts)


async def _success(url: str, key, method_name: str, content: ScrapedContent,
                   errors: dict, output_format: OutputFormat,
                   start_time: float) -> ScrapingResponse:
    logger.info(f"🎉 Successfully retrieved with method: {method_name}")
    if content.multimodal is not None:
        await postprocess_media(content.multimodal)
    response = ScrapingResponse(url=url, content=content, method=method_name, errors=errors,
                                output_format=output_format,
                                retrieval_time=time.time() - start_time)
    cache.put(key, response)
    return response


async def _last_resort(url: str, domain: str, key, session: aiohttp.ClientSession,
                       errors: dict, output_format: OutputFormat,
                       max_video_size: Optional[int], start_time: float,
                       auto: bool) -> ScrapingResponse:
    """For a URL that no live method can even try (its host is down or refuses this
    server): the archived copy, if there is one, or else the failure."""
    if auto and (archived := await _archive_fallback(url, domain, session, errors,
                                                     output_format, max_video_size)):
        return await _success(url, key, ARCHIVE_FALLBACK, archived, errors, output_format,
                              start_time)
    return _failure(url, output_format, errors, start_time)


ARCHIVE_ORG = "Internet Archive"
ARCHIVE_FALLBACK = f"{ARCHIVE_ORG} (fallback)"


async def _archive_fallback(url: str, domain: str, session: aiohttp.ClientSession,
                            errors: dict, output_format: OutputFormat,
                            max_video_size: Optional[int]) -> Optional[ScrapedContent]:
    """The latest snapshot of the URL in the Wayback Machine, retrieved through the
    Internet Archive integration: the last resort for a page no live method got, e.g.
    because its host is down or refuses this server (archive.premier.gov.ru). Runs only
    after everything else failed, so it costs a healthy URL nothing. Its outcome is
    judged like any method's, so an archived CAPTCHA or teaser does not count either;
    a failure is filed into `errors`.

    Only for the open web: integrations' domains (social media, other archives) need
    their own handling, and their snapshots mostly show a login wall. archive.today is
    not asked: its CAPTCHA gate would turn every such request into a queued challenge."""
    if domain in DOMAIN_TO_INTEGRATION or not is_enabled(ARCHIVE_ORG):
        return None
    snapshot = await _latest_wayback_snapshot(url, session)
    if snapshot is None:
        return None
    logger.info(f"No live method got {url}; trying its Wayback Machine snapshot {snapshot}.")
    try:
        content = await retrieve_via_integration(snapshot, integration_name=ARCHIVE_ORG,
                                                 session=session, output_format=output_format,
                                                 max_video_size=max_video_size)
    except Exception as e:
        errors[ARCHIVE_FALLBACK] = e
        return None
    status, result = _classify(snapshot, ARCHIVE_FALLBACK, content, output_format)
    if status == "success":
        return result
    errors[ARCHIVE_FALLBACK] = result if status == "error" else RetrievalFailed(
        f"The snapshot {snapshot} did not provide the content as {output_format}.")
    return None


async def _latest_wayback_snapshot(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    """The URL of the most recent successful (HTTP 200) capture of `url`, if any.

    Asks both of the Wayback Machine's indexes at once, as each misses what the other
    finds: the availability API had no capture of a thip.media page that CDX listed,
    and CDX regularly takes longer than half a minute to answer, or answers with its
    "Temporarily Offline" page. CDX matches the URL regardless of scheme, `www.` and a
    trailing slash; the availability API does not, so it is asked for the other `www.`
    form too if nothing turned up. Answers are cached for a while, and only a couple of
    lookups run at once, so a batch of failing URLs does not flood archive.org (which
    rate-limits with HTTP 429) or crowd out the Internet Archive integration."""
    query = re.sub(r"^https?://", "", url)  # With the scheme, the API often finds nothing
    key = _wayback_key(query)
    if (cached := _wayback_cache.get(key)) and cached[0] > time.monotonic():
        return cached[1]
    if time.monotonic() < _wayback_backoff[0]:
        return None  # Rate-limited a moment ago: give archive.org a rest

    async with _wayback_gate:
        found, answered = [], True
        for result in await asyncio.gather(_wayback_available(query, session),
                                           _wayback_cdx(query, session)):
            answered &= result is not _FAILED
            if result and result is not _FAILED:
                found.append(result)
        if not found:
            other = query[4:] if query.startswith("www.") else f"www.{query}"
            result = await _wayback_available(other, session)
            answered &= result is not _FAILED
            if result and result is not _FAILED:
                found.append(result)

    snapshot = None
    if found:
        timestamp, original = max(found)  # The newest
        snapshot = f"https://web.archive.org/web/{timestamp}/{original}"
    if snapshot or answered:  # Not a lookup that failed: that one is worth repeating
        ttl = WAYBACK_HIT_TTL if snapshot else WAYBACK_MISS_TTL
        _wayback_cache[key] = (time.monotonic() + ttl, snapshot)
    return snapshot


WAYBACK_TIMEOUT = aiohttp.ClientTimeout(total=20)
WAYBACK_HIT_TTL = 60 * 60
WAYBACK_MISS_TTL = 10 * 60
WAYBACK_BACKOFF = 60  # Seconds without lookups after archive.org answered HTTP 429
_wayback_gate = asyncio.Semaphore(2)
_wayback_cache: dict[str, tuple[float, Optional[str]]] = {}
_wayback_backoff = [0.0]
_FAILED = object()  # A lookup that got no valid answer, as opposed to "no capture"


def _wayback_key(query: str) -> str:
    """The form under which a URL's lookup is cached: variants CDX treats as one."""
    query = query.lower()
    return (query[4:] if query.startswith("www.") else query).rstrip("/")


async def _wayback_json(endpoint: str, params: dict, session: aiohttp.ClientSession):
    """GETs a JSON answer from archive.org, or `_FAILED`. A rate limit (429), a server
    error or a non-JSON body (the "Temporarily Offline" page) is retried once, after a
    short pause (a timeout is not); a second 429 suspends all lookups for WAYBACK_BACKOFF seconds."""
    for attempt in range(2):
        try:
            async with session.get(endpoint, params=params, timeout=WAYBACK_TIMEOUT) as response:
                if response.status == 200:
                    try:
                        return await response.json(content_type=None)
                    except ValueError:
                        problem = "a page that is not JSON (archive.org offline?)"
                else:
                    problem = f"HTTP {response.status}"
                    if response.status == 429 and attempt:
                        _wayback_backoff[0] = time.monotonic() + WAYBACK_BACKOFF
                    elif 400 <= response.status < 500 and response.status != 429:
                        break  # Asking again will not change that
        except asyncio.TimeoutError:
            # Already cost the full timeout: another go would only double that
            logger.debug(f"Wayback lookup {endpoint} for {params.get('url')} timed out.")
            break
        except Exception as e:
            problem = f"{type(e).__name__}: {e}"
        logger.debug(f"Wayback lookup {endpoint} for {params.get('url')} got {problem}.")
        if not attempt:
            await asyncio.sleep(3)
    return _FAILED


async def _wayback_available(query: str, session: aiohttp.ClientSession):
    """(timestamp, original URL) of the capture the availability API names, None if it
    names none, or `_FAILED`."""
    data = await _wayback_json("https://archive.org/wayback/available", {"url": query}, session)
    if data is _FAILED or not isinstance(data, dict):
        return _FAILED
    closest = (data.get("archived_snapshots") or {}).get("closest") or {}
    match = re.match(r"https?://web\.archive\.org/web/(\d+)/(.+)", closest.get("url") or "")
    if closest.get("available") and str(closest.get("status")) == "200" and match:
        return match.group(1), match.group(2)
    return None


async def _wayback_cdx(query: str, session: aiohttp.ClientSession):
    """(timestamp, original URL) of the newest HTTP 200 capture in the CDX index, None if
    there is none, or `_FAILED`. Filtered here rather than by the server: its `filter`
    makes it scan far more slowly."""
    params = {"url": query, "limit": "-10", "output": "json", "fl": "timestamp,original,statuscode"}
    rows = await _wayback_json("https://web.archive.org/cdx/search/cdx", params, session)
    if rows is _FAILED or not isinstance(rows, list):
        return _FAILED
    captures = [(row[0], row[1]) for row in rows[1:] if len(row) == 3 and row[2] == "200"]
    return max(captures) if captures else None


def _record_outcome(method_name: str, status: str, payload, errors: dict, partial,
                    output_format: OutputFormat):
    """Files one method's outcome into the error dict / partial content.
    Returns the (possibly updated) partial content."""
    if status == "partial":
        if output_format == "html":
            # The only format a method can lack: Markdown is derived from the sequence
            message = (f"Method {method_name} has no HTML page to offer (it retrieves the "
                       f"content through an API or a player); request 'markdown' or "
                       f"'multimodal' instead.")
        else:
            message = f"Method {method_name} did not provide the content as {output_format}."
        errors[method_name] = RetrievalFailed(message)
        return partial or payload
    errors[method_name] = payload
    return partial


async def _run_methods_sequentially(
        methods: list[str],
        evaluate: Callable[[str], Coroutine],
        output_format: OutputFormat,
) -> tuple[Optional[tuple[str, ScrapedContent]], dict, Optional[ScrapedContent]]:
    """Runs the methods strictly one after another, stopping at the first success."""
    errors: dict[str, Optional[Exception]] = {}
    partial = None

    for method_name in methods:
        status, payload = await evaluate(method_name)
        if status == "success":
            return (method_name, payload), errors, partial
        partial = _record_outcome(method_name, status, payload, errors, partial, output_format)
        if isinstance(payload, TargetUnavailableError):
            break  # Not worth trying other methods

    return None, errors, partial


async def _run_methods_hedged(
        methods: list[str],
        evaluate: Callable[[str], Coroutine],
        delay: float,
        output_format: OutputFormat,
) -> tuple[Optional[tuple[str, ScrapedContent]], dict, Optional[ScrapedContent]]:
    """Runs the methods with hedging: each method gets `delay` seconds of head start
    before the next one is launched alongside it, and a method that fails early hands
    over immediately. The first success wins and the remaining methods are cancelled.

    This trades duplicated work for latency: a method that is merely slow no longer
    forces every later method to wait for its full timeout budget.
    """
    errors: dict[str, Optional[Exception]] = {}
    partial = None
    winner = None
    remaining = list(methods)
    pending: dict[asyncio.Task, str] = {}

    try:
        while remaining or pending:
            if remaining:
                method_name = remaining.pop(0)
                pending[asyncio.create_task(evaluate(method_name))] = method_name

            # Wait for the head start to elapse, or indefinitely once everything is running
            done, _ = await asyncio.wait(
                pending, timeout=delay if remaining else None,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                method_name = pending.pop(task)
                try:
                    status, payload = task.result()
                except asyncio.CancelledError:
                    continue
                except (DiskFull, sqlite3.OperationalError):
                    raise  # Fatal, just as it is when the methods run sequentially
                except Exception as e:  # Should not happen: evaluate() catches its own
                    errors[method_name] = e
                    continue

                if status == "success":
                    winner = (method_name, payload)
                    break

                partial = _record_outcome(method_name, status, payload, errors, partial,
                                          output_format)
                if isinstance(payload, TargetUnavailableError):
                    remaining.clear()  # Not worth launching further methods

            if winner is not None:
                break

    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    return winner, errors, partial


_TAGS = re.compile(r"<(script|style|noscript)\b.*?</\1>|<[^>]+>", re.DOTALL | re.IGNORECASE)


def _is_empty(content: ScrapedContent, output_format: OutputFormat) -> bool:
    """Whether the content in the requested format carries neither text nor media."""
    value = content.get(output_format)
    if value is None:
        return False  # Not provided at all: judged as "partial" elsewhere
    if output_format == "html":
        html = str(value)
        return not _TAGS.sub(" ", html).strip() and not re.search(r"<(img|video)\b", html, re.I)
    # Media items render as references like <image:12>, so they count as content
    return not str(value).strip()


PLAIN_HTTP = "Plain HTTP"


async def _plain_http(url: str, session: aiohttp.ClientSession, output_format: OutputFormat,
                      max_video_size: Optional[int]) -> ScrapedContent:
    """GETs the page directly, as static HTML. The last resort for the open web: scraping
    services refuse some domains outright (Decodo: gov.ru) or fail where a plain request
    goes through (archive.premier.gov.ru, which Firecrawl could not load). Its result goes
    through the same checks as any method's, so a bot page or an empty JavaScript shell
    does not count as content."""
    async with session.get(url, headers=HEADERS, allow_redirects=True,
                           timeout=aiohttp.ClientTimeout(total=30)) as response:
        if response.status in (404, 410):
            raise TargetUnavailableError(f"{url} does not exist on the live site "
                                         f"(HTTP {response.status}).")
        if response.status != 200:
            raise RetrievalFailed(f"A plain request got HTTP {response.status}.")
        content_type = response.headers.get("content-type", "text/html")
        if "html" not in content_type:
            raise RetrievalFailed(f"A plain request got {content_type}, not a web page.")
        html = await response.text(errors="replace")
        final_url = str(response.url)
    return await to_scraped_content(html, session=session, output_format=output_format,
                                    url=final_url, max_video_size=max_video_size)


async def _cloudflare_fallback(url: str, domain: str, session: aiohttp.ClientSession,
                               errors: dict[str, Optional[Exception]],
                               output_format: OutputFormat,
                               max_video_size: Optional[int]) -> Optional[ScrapedContent]:
    """Last resort for a URL behind a Cloudflare challenge when the Browser method was not
    among the methods tried (e.g. a domain routed to Firecrawl or Decodo only): the
    browser gets past the challenge where scraping services cannot (see
    `HeadedBrowser._pass_cloudflare()`). Those services do not even report the challenge
    as one -- Firecrawl fails with a 500, Decodo with a 400 -- so whether it is one is
    asked of Cloudflare itself. Its failure is filed into `errors`; a challenge it could
    not pass then goes to the CAPTCHA queue like any other."""
    if domain in DOMAIN_TO_INTEGRATION or not is_enabled(BROWSER):
        return None  # Integrations handle their domains' checks themselves
    if not await _behind_cloudflare_challenge(url, session, errors):
        return None

    logger.info(f"☁️ {url} is behind a Cloudflare challenge; trying the browser.")
    try:
        content = await browser._get(url, output_format=output_format,
                                     max_video_size=max_video_size)
    except Exception as e:
        errors[BROWSER] = e
        return None
    if captcha := detect_captcha(content):
        errors[BROWSER] = CaptchaEncounteredError(
            f"Method {BROWSER} encountered a {captcha}.")
        return None
    if content.get(output_format) is None:
        errors[BROWSER] = RetrievalFailed(
            f"Method {BROWSER} did not provide the content as {output_format}.")
        return None
    return content


async def _behind_cloudflare_challenge(url: str, session: aiohttp.ClientSession,
                                       errors: dict[str, Optional[Exception]]) -> bool:
    """Whether a method saw a Cloudflare challenge, or Cloudflare answers with one now:
    it marks its challenge responses with `cf-mitigated: challenge`."""
    if any(isinstance(e, CaptchaEncounteredError) and "cloudflare" in str(e).lower()
           for e in errors.values()):
        return True
    try:
        async with session.get(url, headers=HEADERS, allow_redirects=True,
                               timeout=aiohttp.ClientTimeout(total=10)) as response:
            return response.headers.get("cf-mitigated", "").lower() == "challenge"
    except Exception:
        return False


def _record_challenge(domain: str, url: str, errors: dict[str, Optional[Exception]],
                      output_format: OutputFormat, methods: list[str],
                      max_video_size: Optional[int]) -> None:
    """Opens (or joins) a CAPTCHA challenge for the domain if a method ran into one.
    A human decides on it in the web UI: solve it, or discard it -- which blacklists the
    domain, as used to happen straight away. Integrations that queue their own gated
    requests (Archive.today) are left to do so.

    Only a CAPTCHA the shared browser met counts (the Browser method's, or a browser-
    based integration's): a human solves it in that browser, and the clearance helps
    nothing else. Firecrawl's or Decodo's CAPTCHAs used to queue domains that the
    browser's own Cloudflare solver passes -- thip.media, after the browser merely timed
    out once -- and every later URL of those domains then failed at once, for hours."""
    error = next((e for m, e in errors.items() if isinstance(e, CaptchaEncounteredError)
                  and (m == BROWSER or m in INTEGRATION_NAMES)), None)
    if error is None:
        return
    integration = DOMAIN_TO_INTEGRATION.get(domain)
    if getattr(integration, "handles_captchas", False):
        return
    challenges.store.record(domain, url, _captcha_name(error), output_format, methods,
                            max_video_size)


def _captcha_name(error: Exception) -> str:
    """"Method X encountered a Cloudflare challenge." -> "Cloudflare challenge"."""
    message = str(error)
    if " encountered a " in message:
        return message.split(" encountered a ", 1)[1].split(" at ")[0].rstrip(".")
    return "CAPTCHA"


def _local_methods() -> set[str]:
    """The methods that fetch pages from this server's own network, so that a host this
    server cannot connect to is out of their reach: the browser, plain requests, and
    Firecrawl when it is self-hosted (on this machine or its private network)."""
    local = {BROWSER, PLAIN_HTTP}
    instances = fire.firecrawl_urls or configured_firecrawl_urls()
    if all(_is_private_host(urlsplit(u).hostname or "") for u in instances):
        local.add("firecrawl")
    return local


def _is_private_host(host: str) -> bool:
    if host == "localhost" or "." not in host:  # Also Docker service names
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


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


def applicable_methods(url: str, allowed_methods: Literal["auto"] | list[str]) -> list[str]:
    """Every retrieval method that could handle this URL, in the order to try them,
    whether or not the deployment has switched it off."""
    # Initialize methods list
    methods = (get_optimal_methods(url) if allowed_methods == "auto"
               else [resolve_alias(m) for m in allowed_methods])

    # Resolve 'integrations' method to specific, applicable integrations, maintaining order
    methods_resolved = []
    for method in methods:
        if method == "integrations":
            methods_resolved.extend(get_integrations_for_url(url))
        else:
            methods_resolved.append(method)

    return methods_resolved


def resolve_best_methods(url: str, allowed_methods: Literal["auto"] | list[str]) -> list[str]:
    """The methods that will actually be tried for this URL.

    Methods the deployment has switched off are dropped here rather than left to fail
    their way down the list: a disabled method should cost nothing at all.
    """
    return filter_methods(applicable_methods(url, allowed_methods))


async def postprocess_media(result: MultimodalSequence):
    """Ensure all media are located in the default ezmm directory (no temp files)
    and transcode all videos into a format suitable for browser playback.

    Both steps touch the file system and run FFmpeg, so they are kept off the event
    loop: otherwise one video would stall every retrieval running concurrently.
    """
    await asyncio.to_thread(_relocate_items, result)
    from scrapemm.server.environment import ffmpeg_available
    if ffmpeg_available:
        await asyncio.gather(*(normalize_video(video) for video in result.videos))


def _relocate_items(result: MultimodalSequence):
    """Moves every item into the ezmm registry directory."""
    for item in result.unique_items():
        item.relocate(move_not_copy=True)


def get_optimal_methods(url: str) -> list[str]:
    """Returns the best retrieval methods for the given URL."""
    domain = get_domain(url)
    methods = BEST_METHODS.get(domain, METHODS).copy()
    if looks_like_pdf_url(url):
        # The browser only shows a PDF, it cannot extract its text. Firecrawl parses
        # PDFs itself, and Decodo does too.
        methods = [m for m in methods if m != BROWSER]
    return methods
