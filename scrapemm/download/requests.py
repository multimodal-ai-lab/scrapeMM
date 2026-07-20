import logging
from typing import Optional, Union, TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from playwright.async_api import APIRequestContext

from scrapemm.download.common import ssl_context, RELAXED_SSL_DOMAINS
from scrapemm.download.util import stream

logger = logging.getLogger("scrapeMM")

# Cloudflare (and similar) bot gates reject aiohttp's TLS fingerprint with HTTP 403
# while accepting real browser JA3/JA4 profiles. Tried in order until one succeeds.
_CURL_CFFI_IMPERSONATIONS = ("chrome124", "safari17_2_ios", "chrome110")


async def _request_via_curl_cffi(
        url: str,
        headers: Optional[dict] = None,
) -> Optional[tuple[int, dict, bytes]]:
    """GET ``url`` with browser TLS impersonation. Returns (status, headers, body) or None."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        logger.debug("curl_cffi not available; cannot bypass bot-gated 403 for %s", url)
        return None

    async with AsyncSession() as session:
        for impersonate in _CURL_CFFI_IMPERSONATIONS:
            try:
                response = await session.get(
                    url,
                    impersonate=impersonate,
                    headers=headers or {},
                    allow_redirects=True,
                )
                status = response.status_code
                hdrs = dict(response.headers)
                body = response.content
                if status == 200:
                    logger.debug(
                        "Retrieved %s via curl_cffi impersonate=%s (%s bytes)",
                        url, impersonate, len(body),
                    )
                    return status, hdrs, body
                if status != 403:
                    # Real client/server error — further fingerprints won't help.
                    return status, hdrs, body
                logger.debug(
                    "curl_cffi impersonate=%s still got 403 for %s; trying next profile",
                    impersonate, url,
                )
            except Exception:
                logger.debug(
                    "curl_cffi impersonate=%s failed for %s",
                    impersonate, url, exc_info=True,
                )
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


async def fetch_headers(url, session: Union[aiohttp.ClientSession, "APIRequestContext"], **kwargs) -> dict:
    """Fetch only HTTP headers for a URL."""
    from scrapemm.util import get_domain
    ssl = None if get_domain(str(url)) in RELAXED_SSL_DOMAINS else ssl_context

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
                response = await session.head(str(url), headers=kwargs.get("headers"), timeout=kwargs.get("timeout"))
                if getattr(response, "status", None) == 403:
                    return await _headers_via_curl_cffi()
                return response.headers
            else:
                # aiohttp
                async with session.head(url, ssl=ssl, **kwargs) as response:
                    response.raise_for_status()
                    return dict(response.headers)
        except Exception:
            logger.debug(f"HEAD failed for {url}, falling back to GET", exc_info=True)
            # Fallback to GET
            try:
                if not isinstance(session, aiohttp.ClientSession):
                    response = await session.get(str(url), headers=kwargs.get("headers"), timeout=kwargs.get("timeout"))
                    if getattr(response, "status", None) == 403:
                        return await _headers_via_curl_cffi()
                    return response.headers
                else:
                    async with session.get(url, ssl=ssl, **kwargs) as response:
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
                         **kwargs) -> Optional[str | bytes]:
    """Downloads the static page from the given URL using aiohttp or Playwright.

    On HTTP 403 (common Cloudflare bot gate), retries with curl_cffi browser
    TLS impersonation so hosts that accept browsers but reject aiohttp still work.
    """
    if not url:
        return None

    url = str(url)
    from scrapemm.util import get_domain
    ssl = None if get_domain(url) in RELAXED_SSL_DOMAINS else ssl_context

    async def _from_curl_cffi() -> Optional[str | bytes]:
        result = await _request_via_curl_cffi(url, _merge_request_headers(session, kwargs))
        if not result or result[0] != 200:
            return None
        _, _, content = result
        if get_text:
            return content.decode("utf-8", errors="replace")
        return content

    try:
        if not isinstance(session, aiohttp.ClientSession):
            # Playwright APIRequestContext
            response = await session.get(url, **kwargs)
            if response.ok:
                return await response.text() if get_text else await response.body()
            if response.status == 403:
                return await _from_curl_cffi()
            return None
        else:
            # aiohttp
            async with session.get(url, timeout=10, allow_redirects=True,
                                   raise_for_status=True, ssl=ssl, **kwargs) as response:
                if get_text:
                    return await response.text()
                else:
                    return await stream(response)
    except aiohttp.ClientResponseError as e:
        if e.status == 403:
            content = await _from_curl_cffi()
            if content is not None:
                return content
        logger.debug(f"Error requesting {url}", exc_info=True)
        return None
    except Exception:
        logger.debug(f"Error requesting {url}", exc_info=True)
        return None
