"""Tests the non-persistent scraping cache. Uses a URL from test_generic_retrieval."""

import time

import pytest

from scrapemm import retrieve, set_cache_ttl, clear_cache
from scrapemm.common.cache import cache, cache_key, DEFAULT_CACHE_TTL
from scrapemm.retrieval import resolve_best_methods

URL = "https://www.vishvasnews.com/viral/fact-check-upsc-has-not-reduced-the-maximum-age-limit-for-ias-and-ips-exams/"


@pytest.fixture(autouse=True)
def fresh_cache():
    """Runs each test with an empty cache and the default caching duration."""
    clear_cache()
    set_cache_ttl(DEFAULT_CACHE_TTL)
    yield
    clear_cache()
    set_cache_ttl(DEFAULT_CACHE_TTL)


@pytest.mark.asyncio
async def test_cache_hit():
    first = await retrieve(URL)
    assert first.success, first.errors
    assert not first.from_cache
    assert len(cache) == 1

    second = await retrieve(URL)
    assert second.success
    assert second.from_cache
    assert second.method == first.method
    assert second.content is first.content  # Nothing was scraped or downloaded again
    assert second.retrieval_time < first.retrieval_time
    assert len(cache) == 1

    # A different output format is scraped again and cached separately
    html = await retrieve(URL, output_format="html")
    assert html.success, html.errors
    assert not html.from_cache
    assert len(cache) == 2


@pytest.mark.asyncio
async def test_cache_bypass():
    first = await retrieve(URL)
    assert first.success, first.errors

    fresh = await retrieve(URL, use_cache=False)
    assert fresh.success, fresh.errors
    assert not fresh.from_cache
    assert fresh.content is not first.content

    # The bypassing retrieval refreshed the cache entry
    assert len(cache) == 1
    assert (await retrieve(URL)).content is fresh.content


@pytest.mark.asyncio
async def test_cache_expiry():
    response = await retrieve(URL)
    assert response.success, response.errors

    key = cache_key(response.url, response.output_format, resolve_best_methods(response.url, "auto"))
    assert cache.get(key) is response

    set_cache_ttl(0.5)
    assert cache.get(key) is response
    time.sleep(0.6)
    assert cache.get(key) is None
    assert len(cache) == 0


@pytest.mark.asyncio
async def test_caching_disabled():
    set_cache_ttl(0)
    response = await retrieve(URL)
    assert response.success, response.errors
    assert not response.from_cache
    assert len(cache) == 0
