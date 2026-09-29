"""The server side of a search provider: what calls its API with the server's key.

A search costs the server almost nothing, but it costs the provider a request, and
providers limit how many they take at once. So searches pass one server-wide gate, as
retrievals do (`engine._concurrency_gate`): at most `max_search_concurrency` are with a
provider at a time, and the rest queue rather than all hitting it in the same second and
failing on its rate limit together. Each provider also keeps one HTTP session, so that
searches reuse its connections instead of opening a fresh TLS connection apiece.
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Generic, Optional, TypeVar

import aiohttp

from scrapemm.common.paths import APP_NAME
from scrapemm.search import SearchQuery, SearchResponse
from ..config import get_config_var
from ..secrets import is_set
from ..toggles import is_enabled

logger = logging.getLogger(APP_NAME)

Q = TypeVar("Q", bound=SearchQuery)
R = TypeVar("R", bound=SearchResponse)


class ProviderNotConfigured(RuntimeError):
    """The provider's API key (or another secret it needs) is not set up."""


class ProviderDisabled(RuntimeError):
    """The provider was switched off on the dashboard."""


# Searches with a provider at once, server-wide; override with
# `update_config(max_search_concurrency=...)`
DEFAULT_MAX_SEARCH_CONCURRENCY = 10

_gate: Optional[asyncio.Semaphore] = None
_gate_limit: Optional[int] = None


def search_gate() -> asyncio.Semaphore:
    """The limit on searches in flight. Rebuilt when the setting changes, like the
    retrieval gate: searches already inside finish under the old limit."""
    global _gate, _gate_limit
    configured = get_config_var("max_search_concurrency")
    limit = max(1, int(DEFAULT_MAX_SEARCH_CONCURRENCY if configured is None else configured))
    if _gate is None or _gate_limit != limit:
        _gate, _gate_limit = asyncio.Semaphore(limit), limit
    return _gate


class SearchProvider(ABC, Generic[Q, R]):
    """Answers the queries of one search API. `name` must equal the query class's
    `provider`, since that is how the client addresses the provider: POST
    /v1/search/<name>."""

    name: str
    label: str  # How the UI and messages call it
    homepage: str
    query_class: type[Q]
    secret_names: tuple[str, ...] = ()

    _session: Optional[aiohttp.ClientSession] = None
    _session_loop: Optional[asyncio.AbstractEventLoop] = None

    def missing_secrets(self) -> list[str]:
        return [name for name in self.secret_names if not is_set(name)]

    def is_configured(self) -> bool:
        return not self.missing_secrets()

    def is_enabled(self) -> bool:
        return is_enabled(self.name)

    def describe(self) -> dict:
        missing = self.missing_secrets()
        return {
            "name": self.name,
            "label": self.label,
            "homepage": self.homepage,
            "required_secrets": list(self.secret_names),
            "missing_secrets": missing,
            "configured": not missing,
            "enabled": self.is_enabled(),
        }

    async def search(self, query: Q,
                     session: Optional[aiohttp.ClientSession] = None) -> R:
        """Runs one search, through the gate. `session` is only for callers that bring
        their own (tests do); the provider's shared one is used otherwise."""
        # Refusals first: a query that cannot be answered must not wait for a slot
        query.validate()
        if not self.is_enabled():
            raise ProviderDisabled(f"{self.label} is disabled. Enable it on the dashboard "
                                   f"to search with it.")
        if missing := self.missing_secrets():
            raise ProviderNotConfigured(
                f"{self.label} is not configured: set {', '.join(missing)} under Secrets "
                f"in the web UI.")

        queued = time.time()
        # Retries happen inside: a provider pushing back slows down every search
        async with search_gate():
            started = time.time()
            if (waited := started - queued) >= 1:
                logger.debug(f"A {self.label} search waited {waited:.1f}s for a slot.")
            response = await self._search(query, session or self.session())
        logger.info(f"🔎 {self.label} {self.summarize(query)}: {len(response.urls)} "
                    f"result(s) in {time.time() - started:.1f}s.")
        return response

    def session(self) -> aiohttp.ClientSession:
        """The provider's HTTP session, made on first use. A new one replaces it if it
        was closed or belongs to another event loop (which only tests switch between)."""
        loop = asyncio.get_running_loop()
        if self._session is None or self._session.closed or self._session_loop is not loop:
            self._session = aiohttp.ClientSession()
            self._session_loop = loop
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def summarize(self, query: Q) -> str:
        """The query in a few words, for the log."""
        return repr(query)

    @abstractmethod
    async def _search(self, query: Q, session: aiohttp.ClientSession) -> R:
        """Calls the provider's API. Failures are raised as the exceptions that
        describe them (RateLimitError, QuotaExceededError, TimeoutError, ValueError
        for a refused query, RuntimeError for the provider failing), which the route
        turns into matching status codes."""
