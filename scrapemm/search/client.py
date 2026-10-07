"""The client side of search: send a provider's query to the server, get that
provider's response back.

One search is one short request, so unlike `retrieve()` there is no stream: the server
answers with the provider's response as a single JSON object.
"""

import asyncio
import json
import logging
from typing import Any, Collection, Optional, overload

import aiohttp

from scrapemm.client.client import _describe_failure, _headers, _identify
from scrapemm.client.settings import Settings, settings
from scrapemm.common import APP_NAME
from scrapemm.common.exceptions import WIRE_EXCEPTIONS, ServerError, exception_from_wire
from .base import R, SearchQuery

logger = logging.getLogger(APP_NAME)


@overload
async def search(query: SearchQuery[R], config: Optional[Settings] = None) -> R: ...


@overload
async def search(query: Collection[SearchQuery[R]],
                 config: Optional[Settings] = None) -> list[R]: ...


async def search(query: SearchQuery[R] | Collection[SearchQuery[R]],
                 config: Optional[Settings] = None) -> R | list[R]:
    """Runs a search through a scrapeMM server and returns the provider's response.

    The query's class says which provider answers it, and the response comes back as
    that provider's own response class:

    >>> from scrapemm.search import SerperQuery, search
    >>> response = await search(SerperQuery(q="eiffel tower", type="images"))
    >>> response.images[0].image_url

    Pass a list of queries to run them concurrently and get a list of responses back,
    in the same order:

    >>> responses = await search([SerperQuery(q="eiffel tower"), SerperQuery(q="louvre")])

    Every response has `urls`, which can go straight into `scrapemm.retrieve()`.

    :param query: The query, e.g. a `SerperQuery`, or a list of queries.
    :param config: Connection settings to use instead of the global ones.
    :raises ValueError: A query is invalid, or the provider refused it.
    :raises RateLimitError: The provider's rate limit was hit.
    :raises QuotaExceededError: The provider's credits or quota are used up.
    :raises TimeoutError: The provider did not answer in time.
    :raises ServerError: The server could not be reached, or could not search (for
        instance because the provider's API key is not set up in its web UI).
    """
    if isinstance(query, SearchQuery):
        return (await search([query], config))[0]
    queries = list(query)

    # Validate all before sending any, so that one bad query does not waste the others
    for q in queries:
        q.validate()
    if not queries:
        return []

    config = config or settings
    timeout = aiohttp.ClientTimeout(total=None, sock_read=config.read_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            results = await asyncio.gather(
                *(_search(session, config, q) for q in queries))
    except aiohttp.ClientError as e:
        raise ServerError(
            f"Could not reach the scrapeMM server at {config.base_url}: {e}") from e
    return results


async def _search(session: aiohttp.ClientSession, config: Settings,
                  query: SearchQuery[R]) -> R:
    """Sends one query and returns the provider's response."""
    url = f"{config.base_url}/v1/search/{query.provider}"
    async with session.post(url, json=query.to_dict(),
                            headers=_headers(config, accept="application/json")
                            ) as response:
        body = await response.text()
        if response.status == 401:
            raise ServerError(
                f"{url} rejected the API key. Set it with "
                f"scrapemm.configure(api_key=...) or SCRAPEMM_API_KEY.")
        if response.status >= 400:
            raise await _failure(session, config, url, response, body)

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise ServerError(f"POST {url} answered with something other than JSON: "
                          f"{body[:300]!r}") from e
    return query.response_class.from_dict(data)


async def _failure(session: aiohttp.ClientSession, config: Settings, url: str,
                   response: aiohttp.ClientResponse, body: str) -> Exception:
    """The exception an error response stands for. The server names the type of each
    failure, so that callers can catch a rate limit as the RateLimitError it is."""
    error: Any = None
    try:
        error = json.loads(body).get("error")
    except (json.JSONDecodeError, AttributeError):
        pass
    if isinstance(error, dict) and error.get("type") in WIRE_EXCEPTIONS:
        return exception_from_wire(error)

    message, _ = _describe_failure(url, response, body)
    # Every failure of the search routes carries an `error`; a 404 without one comes
    # from somewhere else, most likely a server that predates search
    if response.status == 404 and not isinstance(error, dict) and not response.history:
        message += await _identify(session, config.base_url, route="POST /v1/search")
    return ServerError(message)
