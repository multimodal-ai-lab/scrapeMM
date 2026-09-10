"""Simple, non-persistent (in-memory) cache for scraping results."""

import time
from typing import Optional

from .scraping_response import ScrapingResponse, OutputFormat

DEFAULT_CACHE_TTL = 24 * 60 * 60  # 24 hours

# A cache entry is identified by the URL, the requested output format, and the methods used
CacheKey = tuple[str, str, tuple[str, ...]]


def cache_key(url: str, output_format: OutputFormat, methods: list[str]) -> CacheKey:
    """Constructs the cache key identifying a particular retrieval request."""
    return url, output_format, tuple(methods)


class ScrapeCache:
    """Keeps successful scraping results in memory for `ttl` seconds. Not persisted,
    i.e., the cache is empty again after the process terminated."""

    def __init__(self, ttl: float = DEFAULT_CACHE_TTL):
        self.ttl = ttl
        self._entries: dict[CacheKey, tuple[float, ScrapingResponse]] = {}
        self._prune_at = 128  # Number of entries at which to prune expired ones

    def get(self, key: CacheKey) -> Optional[ScrapingResponse]:
        """Returns the cached response for `key` if there is a non-expired one."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        timestamp, response = entry
        if time.time() - timestamp > self.ttl:
            del self._entries[key]
            return None
        return response

    def put(self, key: CacheKey, response: ScrapingResponse) -> None:
        """Caches the given response (unless caching is disabled via `ttl <= 0`)."""
        if self.ttl <= 0:
            return
        if len(self._entries) >= self._prune_at:
            self._prune()
        self._entries[key] = (time.time(), response)

    def clear(self) -> None:
        self._entries.clear()

    def _prune(self) -> None:
        """Drops all expired entries."""
        deadline = time.time() - self.ttl
        self._entries = {k: v for k, v in self._entries.items() if v[0] >= deadline}
        self._prune_at = max(128, 2 * len(self._entries))

    def __len__(self) -> int:
        return len(self._entries)


cache = ScrapeCache()


def set_cache_ttl(seconds: float) -> None:
    """Sets how long (in seconds) scraping results are re-used from the cache.
    Use 0 (or any negative value) to disable caching."""
    cache.ttl = seconds


def clear_cache() -> None:
    """Removes all entries from the scraping cache."""
    cache.clear()
