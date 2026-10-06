"""Screenshots of retrieved pages, taken on request (`retrieve(..., screenshot=True)`).

Taken in the server's shared browser after the retrieval, in a tab of their own, for
whatever method retrieved the page: the screenshot shows the page as a visitor sees it,
also where the content came from an API. The tab counts against the browser's page
limits like any retrieval.

What is captured is the top of the page, up to three screens high, at desktop width:
a full page of an endless news site would only be shrunk to an unreadable strip
(images are kept at 2048 px at most).

Screenshots are cached by URL for as long as results are (see `cache`), separately from
the results: a request with and one without a screenshot share the retrieval.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from contextlib import suppress
from typing import Optional

from ezmm import Image

from scrapemm.common.paths import APP_NAME

logger = logging.getLogger(APP_NAME)

WIDTH, HEIGHT = 1920, 1080
MAX_SCREENS = 3
TIMEOUT = 90  # Seconds for loading and capturing
SETTLE_TIMEOUT = 5  # Seconds to wait for the page to stop loading (images, charts)
MAX_CACHED = 256

_cached: OrderedDict[str, tuple[float, Image]] = OrderedDict()


async def screenshot(url: str, use_cache: bool = True) -> Optional[Image]:
    """The page at `url` as the server's browser shows it, or None if it could not be
    captured. Never raises: a missing screenshot must not fail a retrieval."""
    from .cache import cache
    if use_cache and cache.active and (hit := _cached.get(url)):
        taken_at, image = hit
        if time.time() - taken_at <= cache.ttl:
            _cached.move_to_end(url)
            return image
        del _cached[url]
    try:
        image = await asyncio.wait_for(_take(url), TIMEOUT)
    except Exception as e:
        logger.info(f"📷 No screenshot of {url}: {type(e).__name__}: {e}")
        return None
    if image is not None and cache.active:
        _cached[url] = (time.time(), image)
        while len(_cached) > MAX_CACHED:
            _cached.popitem(last=False)
    return image


async def _take(url: str) -> Optional[Image]:
    from .download.images import decode_image
    from .integrations import browser
    from .integrations.headed_browser import _BrowserSlot, release_page
    from .util import get_domain

    slot = _BrowserSlot(get_domain(url) or "")
    await slot.acquire()
    try:
        page, _ = await browser._new_page(None)
        try:
            await page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
            await page.goto(url, timeout=browser.navigation_timeout * 1000,
                            wait_until="domcontentloaded")
            # Images and charts load after the DOM: given a moment, not forever
            with suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT * 1000)
            height = await page.evaluate("document.documentElement.scrollHeight") or HEIGHT
            png = await page.screenshot(type="png", full_page=True, clip={
                "x": 0, "y": 0, "width": WIDTH, "height": min(int(height), HEIGHT * MAX_SCREENS)})
        finally:
            await release_page(page)
    finally:
        slot.release()
    return await decode_image(png, url, ignore_small_images=False)
