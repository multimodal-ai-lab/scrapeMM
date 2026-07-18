from typing import Optional, Union, TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from playwright.async_api import APIRequestContext

from scrapemm.download.common import ssl_context, RELAXED_SSL_DOMAINS
from scrapemm.download.util import stream

async def fetch_headers(url, session: Union[aiohttp.ClientSession, "APIRequestContext"], **kwargs) -> dict:
    """Fetch only HTTP headers for a URL."""
    from scrapemm.util import get_domain
    ssl = None if get_domain(str(url)) in RELAXED_SSL_DOMAINS else ssl_context

    if hasattr(session, "head"):  # aiohttp.ClientSession or Playwright APIRequestContext
        try:
            # Playwright APIRequestContext.head exists and returns APIResponse
            if not isinstance(session, aiohttp.ClientSession):
                # Playwright
                response = await session.head(str(url), headers=kwargs.get("headers"), timeout=kwargs.get("timeout"))
                return response.headers
            else:
                # aiohttp
                async with session.head(url, ssl=ssl, **kwargs) as response:
                    response.raise_for_status()
                    return dict(response.headers)
        except Exception:
            # Fallback to GET
            if not isinstance(session, aiohttp.ClientSession):
                response = await session.get(str(url), headers=kwargs.get("headers"), timeout=kwargs.get("timeout"))
                return response.headers
            else:
                async with session.get(url, ssl=ssl, **kwargs) as response:
                    response.raise_for_status()
                    return dict(response.headers)
    raise ValueError(f"Unsupported session type: {type(session)}")

async def request_static(url: str,
                         session: Union[aiohttp.ClientSession, "APIRequestContext"],
                         get_text: bool = True,
                         **kwargs) -> Optional[str | bytes]:
    """Downloads the static page from the given URL using aiohttp or Playwright."""
    if not url:
        return None

    url = str(url)
    from scrapemm.util import get_domain
    ssl = None if get_domain(url) in RELAXED_SSL_DOMAINS else ssl_context

    try:
        if not isinstance(session, aiohttp.ClientSession):
            # Playwright APIRequestContext
            response = await session.get(url, **kwargs)
            if response.ok:
                return await response.text() if get_text else await response.body()
            return None
        else:
            # aiohttp
            async with session.get(url, timeout=10, allow_redirects=True,
                                   raise_for_status=True, ssl=ssl, **kwargs) as response:
                if get_text:
                    return await response.text()
                else:
                    return await stream(response)
    except Exception:
        return None
