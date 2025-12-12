import asyncio
from typing import Optional

import aiohttp

from scrapemm.download.common import ssl_context, RELAXED_SSL_DOMAINS, HEADERS
from scrapemm.download.util import stream


async def fetch_headers(url, session: aiohttp.ClientSession, **kwargs) -> dict:
    """Fetch only HTTP headers for a URL.

    Strategy:
    - Try HEAD first (fastest and cheapest).
    - If the server rejects HEAD (405/501), fall back to GET and return headers without reading the body.
    - Apply the project's SSL policy (relaxed on certain domains).
    """
    from scrapemm.util import get_domain

    ssl = None if get_domain(str(url)) in RELAXED_SSL_DOMAINS else ssl_context
    try:
        async with session.head(url, ssl=ssl, **kwargs) as response:
            response.raise_for_status()
            return dict(response.headers)
    except aiohttp.ClientResponseError as e:
        if e.status in (405, 501):
            # Fall back to GET if HEAD is not allowed/implemented
            async with session.get(url, ssl=ssl, **kwargs) as response:
                response.raise_for_status()
                return dict(response.headers)
        raise


async def request_static(url: str,
                         session: aiohttp.ClientSession,
                         get_text: bool = True,
                         **kwargs) -> Optional[str | bytes]:
    """Downloads the static page from the given URL using aiohttp. If `get_text` is True,
    returns the HTML as text. Otherwise, returns the raw binary content (e.g. an image)."""
    # TODO: Handle web archive URLs
    if url:
        url = str(url)
        try:
            from scrapemm.util import get_domain
            ssl = None if get_domain(url) in RELAXED_SSL_DOMAINS else ssl_context
            async with session.get(url, timeout=10, allow_redirects=True,
                                   raise_for_status=True, ssl=ssl, **kwargs) as response:
                if get_text:
                    return await response.text()  # HTML string
                else:
                    return await stream(response)  # Binary data
        except asyncio.TimeoutError:
            pass  # Server too slow
        except UnicodeError:
            pass  # Page not readable
        except (aiohttp.ClientOSError, aiohttp.ClientConnectorError):
            pass  # Page not available anymore
        except aiohttp.ClientResponseError as e:
            if e.status in [403, 404, 429, 500, 502, 503]:
                # 403: Forbidden access
                # 404: Not found
                # 429: Too many requests
                # 500: Server error
                # 502: Bad gateway
                # 503: Service unavailable (e.g. rate limit)
                pass
            else:
                print(f"\rFailed to retrieve page.\n\t{type(e).__name__}: {e}")
        except Exception as e:
            print(f"\rFailed to retrieve page at {url}.\n\tReason: {type(e).__name__}: {e}")
