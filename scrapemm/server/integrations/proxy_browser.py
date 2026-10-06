"""The Browser method through the proxy (see `scrapemm.server.proxy`).

Same shared browser, same retrieval, but each page opens in a browser context of its own,
created with the proxy (`Browser.new_context(proxy=...)`, which Chromium supports over
CDP: the context's traffic, including the media the page then downloads through
`page.context.request`, goes through the proxy). The context is closed with its page.
Such a context starts without the profile's cookies; that is the price of a different
address."""

import asyncio
import logging
from contextlib import suppress
from typing import Optional

from playwright.async_api import Page, Playwright

from scrapemm.common.paths import APP_NAME
from scrapemm.server import proxy
from scrapemm.server.integrations.headed_browser import (Browser, _own, _run_soon,
                                                         _shared_context, _shared_playwright)

logger = logging.getLogger(APP_NAME)


class ProxiedBrowser(Browser):
    """The Browser method, opening its pages through the current attempt's proxy."""

    async def _new_page(self, p: Optional[Playwright] = None, attempts: int = 3) -> tuple[Page, int]:
        via = proxy.route()
        if via is None:
            return await super()._new_page(p, attempts)
        browser, generation = await self._ensure_browser(
            playwright=p or await _shared_playwright())
        if browser is None:
            raise RuntimeError("The shared browser is not connected.")
        connection = (await _shared_context(browser, generation)).browser
        context = await asyncio.wait_for(
            connection.new_context(proxy=via.for_playwright(), ignore_https_errors=True),
            timeout=30)
        try:
            page = await asyncio.wait_for(context.new_page(), timeout=30)
            await _own(page)  # For its target id, should closing it need the endpoint
        except BaseException:
            with suppress(Exception):
                await asyncio.wait_for(context.close(), timeout=10)
            raise
        # The context goes with its page (closed by `release_page()`)
        page.once("close", lambda _: _run_soon(_close(context)))
        logger.debug(f"Opened a browser page through the proxy {via.safe}.")
        return page, generation


async def _close(context) -> None:
    with suppress(Exception):
        await asyncio.wait_for(context.close(), timeout=10)


browser = ProxiedBrowser()
