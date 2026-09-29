import asyncio
import logging
import time
from contextlib import suppress, contextmanager
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Union, TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from playwright.async_api import APIRequestContext

try:
    # At import, i.e. server start: loading curl's native library takes about a second,
    # which inside a request would stall the event loop
    from curl_cffi.requests import Session as CurlSession
except ImportError:
    CurlSession = None

from scrapemm.server.download.common import ssl_context, RELAXED_SSL_DOMAINS, BROWSER_TLS_DOMAINS, HEADERS
from scrapemm.server.download.util import stream, MediaTooLarge

logger = logging.getLogger("scrapeMM")

# Timeouts are on *stalls*, not on total duration: a large video legitimately takes a
# while, but a connection that stops delivering bytes is dead. `sock_read` bounds the
# gap between two chunks, so slow-but-progressing downloads are never cut off, while a
# hung connection still fails fast.
TEXT_TIMEOUT = aiohttp.ClientTimeout(total=60, sock_connect=10, sock_read=15)
MEDIA_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=20)

# Cloudflare (and similar) bot gates reject aiohttp's TLS fingerprint with HTTP 403
# while accepting real browser JA3/JA4 profiles. Tried in order until one succeeds.
# Several builds are listed because some sites (e.g. archive.today) accept one and gate
# another, so trying a spread finds the one that works.
_CURL_CFFI_IMPERSONATIONS = ("chrome124", "chrome", "safari")


# Largest binary body taken through Playwright's request context, see `request_static()`
MAX_PLAYWRIGHT_BODY = 2 * 1024 * 1024

# Per attempt. curl's own default would let a stalled request hold its thread for long.
CURL_CFFI_TIMEOUT = 30

# Their own threads: in the default pool, curl requests would queue behind yt-dlp
# downloads, which can take minutes each. Two pools: media downloads, and page fetches
# and lookups (archive.today pages, Perma.cc's TimeMap, Instagram's API). A page fetch
# that Archive.today tarpits holds its thread for up to CURL_CFFI_TIMEOUT per attempt;
# in a shared pool, a burst of those left every image of a page waiting for a thread.
_curl_executor = ThreadPoolExecutor(max_workers=32, thread_name_prefix="curl-media")
_curl_lookup_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="curl-lookup")


async def _request_via_curl_cffi(
        url: str,
        headers: Optional[dict] = None,
        lookup: bool = False,
) -> Optional[tuple[int, dict, bytes]]:
    """GET ``url`` with browser TLS impersonation. Returns (status, headers, body) or None.

    Runs curl_cffi's *synchronous* client in a worker thread. Its async client drives
    curl's sockets through the server's event loop (`loop.add_reader`), and when the OS
    hands a socket number to curl that the loop still attributes to one of its own
    connections, the loop refuses ("File descriptor N is used by transport"). That
    failure happens inside curl's socket callback and leaves curl_cffi's shared transfer
    loop broken, so every later request through it hung and the server stopped
    recovering. In a thread, curl does its own I/O and never touches the event loop.

    `lookup` marks a page fetch or an API lookup rather than a media download: the two
    run in separate thread pools, so that neither can starve the other.
    """
    from scrapemm.server.workers import run_in
    if CurlSession is None:
        logger.debug("curl_cffi not available; cannot bypass bot-gated 403 for %s", url)
        return None
    if lookup:
        return await run_in(_curl_lookup_executor, "curl lookup",
                            _request_via_curl_cffi_sync, url, headers)
    return await run_in(_curl_executor, "curl media", _request_via_curl_cffi_sync, url, headers)


async def curl_get(url: str, **kwargs):
    """A GET through curl_cffi (browser TLS impersonation), e.g.
    `curl_get(url, impersonate="chrome124", cookies=..., timeout=30)`. Returns curl_cffi's
    response. Always use this rather than curl_cffi's AsyncSession: that one drives curl's
    sockets through the server's event loop, and when the OS reuses a socket number the
    loop still attributes to a closed connection, the loop refuses it ("File descriptor N
    is used by transport") -- which broke other requests at random, and left curl's
    shared transfer loop wedged (see `_request_via_curl_cffi()`). In a worker thread, curl
    does its own I/O and never touches the event loop. Raises ImportError without
    curl_cffi, and whatever curl raises."""
    if CurlSession is None:
        raise ImportError("curl_cffi is not installed.")

    from scrapemm.server.workers import run_in

    def curl_page_get():
        with _thread_session() as session:
            return session.get(url, **kwargs)

    # A page fetch, not a medium: its own pool (see `_curl_lookup_executor`)
    return await run_in(_curl_lookup_executor, "curl lookup", curl_page_get)


@contextmanager
def _thread_session():
    """A curl session for one request, closed right after it.

    NOT kept per worker thread, although that saved a TCP and TLS handshake per image:
    curl sessions left open in the worker threads made every child process the server
    started crash between fork and exec (SIGSEGV in libc, in the child still named
    "uvicorn"). Playwright's Node driver then died at once ("Connection closed while
    reading from the driver") and ffprobe/ffmpeg failed with -11 -- every browser
    retrieval failed, and in production it stayed that way. Reproduced with the built-in
    suite: 4 driver errors and 4 crashed ffprobe runs with sessions kept per thread,
    none with a session per request. Archive.today's media are cached on disk instead
    (`_ArchiveMediaCache`), which spares the repeated downloads altogether."""
    with CurlSession() as session:
        yield session


def _log_curl(url: str, impersonate: str, outcome: str, started: float) -> None:
    """One curl attempt, timed: at INFO when it was slow, so stalls show where they are."""
    took = time.monotonic() - started
    logger.log(logging.INFO if took >= 3 else logging.DEBUG,
               f"⏱️ curl ({impersonate}) {url[:120]}: {outcome} in {took:.1f} s.")


def _request_via_curl_cffi_sync(url: str, headers: Optional[dict]) -> Optional[tuple[int, dict, bytes]]:
    for impersonate in _CURL_CFFI_IMPERSONATIONS:
        started = time.monotonic()
        try:
            with _thread_session() as session:
                response = session.get(
                    url,
                    impersonate=impersonate,
                    headers=headers or {},
                    allow_redirects=True,
                    timeout=CURL_CFFI_TIMEOUT,
                )
                status = response.status_code
                hdrs = dict(response.headers)
                body = response.content
        except Exception as e:
            _log_curl(url, impersonate, f"{type(e).__name__}", started)
            logger.debug("curl_cffi impersonate=%s failed for %s", impersonate, url,
                         exc_info=True)
            if "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower():
                # The host is slow or delays this client, which another fingerprint
                # would only queue into again
                return None
            continue
        _log_curl(url, impersonate, f"HTTP {status}, {len(body)} bytes", started)
        if status == 200:
            return status, hdrs, body
        if status != 403:
            # Real client/server error — further fingerprints won't help.
            return status, hdrs, body
        logger.debug("curl_cffi impersonate=%s still got 403 for %s; trying next profile",
                     impersonate, url)
    return None


def _merge_request_headers(
        session: Union[aiohttp.ClientSession, "APIRequestContext"],
        kwargs: dict,
) -> dict:
    """Combine session-default headers with per-request overrides for curl_cffi."""
    merged: dict = {}
    if isinstance(session, aiohttp.ClientSession):
        merged.update({str(k): str(v) for k, v in session.headers.items()})
    extra = kwargs.get("headers") or {}
    merged.update({str(k): str(v) for k, v in extra.items()})
    return merged


async def fetch_headers(url,
                        session: Union[aiohttp.ClientSession, "APIRequestContext"],
                        timeout: float = 3,
                        **kwargs) -> dict:
    """Fetch only HTTP headers for a URL.

    `timeout` is in **seconds** and is converted to whatever unit the underlying
    client expects (Playwright counts in milliseconds). Probing headers must stay
    cheap: this call only decides how to handle a URL, so it may never become the
    reason a retrieval stalls.
    """
    from scrapemm.server.util import get_domain
    ssl = None if get_domain(str(url)) in RELAXED_SSL_DOMAINS else ssl_context
    kwargs.pop("timeout", None)  # Unit-ambiguous; the explicit parameter wins
    playwright_timeout = timeout * 1000
    aiohttp_timeout = aiohttp.ClientTimeout(total=timeout)

    async def _headers_via_curl_cffi() -> dict:
        result = await _request_via_curl_cffi(str(url), _merge_request_headers(session, kwargs))
        if result and result[0] == 200:
            return result[1]
        raise RuntimeError(f"Forbidden (403) fetching headers for {url}")

    if hasattr(session, "head"):  # aiohttp.ClientSession or Playwright APIRequestContext
        try:
            # Playwright APIRequestContext.head exists and returns APIResponse
            if not isinstance(session, aiohttp.ClientSession):
                # Playwright
                response = await session.head(str(url), headers=kwargs.get("headers"),
                                              timeout=playwright_timeout)
                if getattr(response, "status", None) == 403:
                    return await _headers_via_curl_cffi()
                return response.headers
            else:
                # aiohttp
                async with session.head(url, ssl=ssl, timeout=aiohttp_timeout, **kwargs) as response:
                    response.raise_for_status()
                    return dict(response.headers)
        except Exception:
            logger.debug(f"HEAD failed for {url}, falling back to GET", exc_info=True)
            # Fallback to GET
            try:
                if not isinstance(session, aiohttp.ClientSession):
                    response = await session.get(str(url), headers=kwargs.get("headers"),
                                                 timeout=playwright_timeout)
                    if getattr(response, "status", None) == 403:
                        return await _headers_via_curl_cffi()
                    return response.headers
                else:
                    async with session.get(url, ssl=ssl, timeout=aiohttp_timeout, **kwargs) as response:
                        response.raise_for_status()
                        return dict(response.headers)
            except aiohttp.ClientResponseError as e:
                if e.status == 403:
                    return await _headers_via_curl_cffi()
                raise
    raise ValueError(f"Unsupported session type: {type(session)}")


async def request_static(url: str,
                         session: Union[aiohttp.ClientSession, "APIRequestContext"],
                         get_text: bool = True,
                         max_size: Optional[int] = None,
                         **kwargs) -> Optional[str | bytes]:
    """Downloads the page or medium at `url` (see `_request_static()`). An archived
    medium that could not be downloaded is noted: a capture's result that lacks it is
    not kept for good (see `cache.complete_capture()`)."""
    content = await _request_static(url, session, get_text=get_text, max_size=max_size, **kwargs)
    if not get_text and url and not isinstance(content, bytes):
        from scrapemm.server.cache import note_failed_medium
        note_failed_medium(str(url))
    return content


async def _request_static(url: str,
                          session: Union[aiohttp.ClientSession, "APIRequestContext"],
                          get_text: bool = True,
                          max_size: Optional[int] = None,
                          **kwargs) -> Optional[str | bytes]:
    """Downloads the static page from the given URL using aiohttp or Playwright.

    Media downloads are bounded by `max_size` (in bytes) and by a stall timeout, not
    by a total-duration timeout: a large file may take as long as it needs as long as
    it keeps making progress.

    On HTTP 403 (common Cloudflare bot gate), retries with curl_cffi browser
    TLS impersonation so hosts that accept browsers but reject aiohttp still work.
    """
    if not url:
        return None

    url = str(url)
    from scrapemm.server.util import get_domain
    ssl = None if get_domain(url) in RELAXED_SSL_DOMAINS else ssl_context
    timeout = kwargs.pop("timeout", None) or (TEXT_TIMEOUT if get_text else MEDIA_TIMEOUT)

    async def _from_curl_cffi() -> Optional[str | bytes]:
        result = await _request_via_curl_cffi(url, _merge_request_headers(session, kwargs))
        if not result or result[0] != 200:
            return None
        _, _, content = result
        if max_size is not None and len(content) > max_size:
            logger.debug(f"Discarding {url}: {len(content)} bytes exceed the limit of {max_size}.")
            return None
        if get_text:
            return content.decode("utf-8", errors="replace")
        return content

    # Some hosts (archive.today) serve an nginx decoy to non-browser TLS clients, so their
    # downloads (e.g. a snapshot's rehosted images) go through curl_cffi first; aiohttp is
    # only the fallback if browser impersonation is unavailable or fails.
    if get_domain(url) in BROWSER_TLS_DOMAINS:
        content = await _from_curl_cffi()
        if content is not None:
            return content

    try:
        if not isinstance(session, aiohttp.ClientSession):
            # Playwright APIRequestContext
            response = await session.get(url, **kwargs)
            if response.ok:
                if get_text:
                    return await response.text()
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) <= MAX_PLAYWRIGHT_BODY:
                    return await response.body()
                # Too large, or of unknown size, to pass through Playwright's pipe: its
                # Python client reassembles a message in time quadratic in its size, on
                # the event loop (see `download.browser.READ_CHUNK`). Downloaded directly.
                with suppress(Exception):
                    await response.dispose()
                async with aiohttp.ClientSession(headers=HEADERS) as direct:
                    return await request_static(url, direct, get_text=False,
                                                max_size=max_size, headers=kwargs.get("headers"))
            if response.status == 403:
                return await _from_curl_cffi()
            return None
        else:
            # aiohttp
            async with session.get(url, timeout=timeout, allow_redirects=True,
                                   raise_for_status=True, ssl=ssl, **kwargs) as response:
                if get_text:
                    return await response.text()
                else:
                    return await stream(response, max_size=max_size)

    except MediaTooLarge as e:
        logger.debug(f"Aborted download: {e}")
        return None

    except aiohttp.ClientResponseError as e:
        if e.status == 403:
            content = await _from_curl_cffi()
            if content is not None:
                return content
        logger.debug(f"Error requesting {url}. Reason: {e}")
        return None

    except aiohttp.ClientConnectorError as e:
        logger.debug(f"Error while connecting to {url}. Reason: {e}")
        return None

    except Exception:
        logger.debug(f"Error requesting {url}", exc_info=True)
        return None
