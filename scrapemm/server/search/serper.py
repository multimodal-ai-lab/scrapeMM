"""Serper (serper.dev), Google results as JSON.

A search is interactive, so retries are few and short: a caller waiting on a result
page is better served by a clear error after a few seconds than by the minute of
backoff a batch scrape can afford.
"""

import asyncio
import json
import logging
from typing import Optional

import aiohttp

from scrapemm.common import QuotaExceededError, RateLimitError
from scrapemm.common.paths import APP_NAME
from scrapemm.search import SerperQuery, SerperResponse
from ..secrets import get_secret
from .base import SearchProvider

logger = logging.getLogger(APP_NAME)

SERPER_URL = "https://google.serper.dev"
ENDPOINTS = {"search": "/search", "images": "/images"}

TIMEOUT = 15  # seconds
MAX_RETRIES = 2
RETRY_DELAY = 1.0  # seconds before the first retry, doubling from there
RETRYABLE = {429, 500, 502, 503, 504}


class Serper(SearchProvider[SerperQuery, SerperResponse]):
    name = "serper"
    label = "Serper"
    homepage = "https://serper.dev"
    query_class = SerperQuery
    secret_names = ("serper_api_key",)

    def summarize(self, query: SerperQuery) -> str:
        kind = "image search" if query.type == "images" else "web search"
        return f'{kind} "{query.q}"'

    async def _search(self, query: SerperQuery,
                      session: aiohttp.ClientSession) -> SerperResponse:
        data = await self._call(ENDPOINTS[query.type], query.request_body(), session)
        # -site: covers only what fits Google's word limit, and Google does not always
        # honour it, so excluded sites are also taken out of the answer here
        return SerperResponse.from_dict(data).without_sites(query.excluded_domains)

    async def _call(self, path: str, body: dict, session: aiohttp.ClientSession) -> dict:
        # Read on every call rather than kept: the key can change in the web UI anytime
        headers = {
            "X-API-KEY": get_secret("serper_api_key") or "",
            "Content-Type": "application/json",
        }
        url = SERPER_URL + path
        for attempt in range(MAX_RETRIES + 1):
            last = attempt >= MAX_RETRIES
            try:
                async with session.post(url, json=body, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=TIMEOUT)
                                        ) as response:
                    text = await response.text()
                    if response.status == 200:
                        return _parse(text)
                    if response.status not in RETRYABLE or last:
                        raise _api_error(response.status, response.reason, text)
                    logger.debug(f"Serper answered {response.status}, retrying.")
            # Before ClientConnectionError: aiohttp's ServerTimeoutError is both
            except (asyncio.TimeoutError, TimeoutError):
                raise TimeoutError(f"Serper did not answer within {TIMEOUT}s.") from None
            except aiohttp.ClientConnectionError as e:
                if last:
                    raise RuntimeError(f"Could not reach Serper: {e}") from e
                logger.debug(f"Could not reach Serper ({e}), retrying.")
            except aiohttp.ClientError as e:
                raise RuntimeError(f"The request to Serper failed: {e}") from e
            await asyncio.sleep(RETRY_DELAY * 2 ** attempt)
        raise RuntimeError("Serper could not be queried.")  # Not reached


def _parse(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        # Not a ValueError: that would read as "your query was invalid"
        raise RuntimeError(f"Serper answered with something other than a JSON object: "
                           f"{text[:200]!r}")
    return data


def _serper_message(body: str) -> str:
    """Serper's own explanation of an error ({"message": "...", "statusCode": 403}),
    else the body itself."""
    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()[:300]
    if isinstance(data, dict):
        for key in ("message", "error", "detail"):
            if data.get(key):
                return str(data[key]).strip()[:300]
    return body.strip()[:300]


def _api_error(status: int, reason: Optional[str], body: str) -> Exception:
    """Turns an error response of Serper into an exception that says what went wrong,
    and what to do about it."""
    message = _serper_message(body)
    said = f' Serper says: "{message}"' if message else ""

    # Serper reports exhausted credits as a 400, so this goes by the message first
    if status == 402 or "credit" in message.lower():
        return QuotaExceededError(f"Serper has no credits left for this search "
                                  f"({status}).{said} Top up at serper.dev.")
    match status:
        case 401 | 403:
            return RuntimeError(f"Serper rejected the API key ({status}).{said} Check "
                                f"serper_api_key under Secrets in the web UI.")
        case 400:
            return ValueError(f"Serper rejected the query as invalid (400).{said}")
        case 429:
            return RateLimitError(f"Serper's rate limit was hit (429), even after "
                                  f"{MAX_RETRIES} retries.{said}")
    return RuntimeError(f"Serper answered {status} {reason or ''}.".replace(" .", ".")
                        + said)
