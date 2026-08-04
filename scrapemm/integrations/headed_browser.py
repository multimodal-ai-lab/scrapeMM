import asyncio
import logging
import sys
from typing import Optional, ClassVar

from ezmm import MultimodalSequence
from playwright.async_api import async_playwright, Page, Frame, ElementHandle, Playwright, \
    BrowserContext, Error as PlaywrightError
from seleniumbase import cdp_driver
from seleniumbase.undetected.cdp_driver.browser import Browser

from scrapemm import RetrievalFailed
from scrapemm.common.retrieval_integration import RetrievalIntegration

logger = logging.getLogger("scrapeMM")

ContentTarget = Page | Frame | ElementHandle


class HeadedBrowser(RetrievalIntegration):
    """Base class for retrieval integrations that need a headed browser to avoid bot blocking
     mechanisms (e.g., Cloudflare) when retrieving web content.
    Serves itself as a generic retrieval integration for platforms that don't need special handling."""
    name = "Headed Browser"
    domains = ["mvau.lt"]

    # Shared UC browser for all HeadedBrowser integrations (Perma.cc, Archive.org, …).
    _browser: ClassVar[Optional[Browser]] = None
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    async def _connect(self):
        """Establishes a connection to a persistent UC browser. Playwright connects
        over CDP per request."""
        async with self._lock:
            await self._connect_locked_internal()
            self.connected = HeadedBrowser._browser is not None

    async def _reconnect(self):
        """Like _connect(), but removes any previously existing connections"""
        async with self._lock:
            self._cleanup_resources()
            await self._connect_locked_internal()
            self.connected = HeadedBrowser._browser is not None

    async def _prepare_context(self, context: BrowserContext) -> None:
        """Optional hook before a new page is created (e.g. inject cookies)."""
        return

    async def _settle_after_goto(self, page: Page) -> None:
        """Optional post-navigation settle. Override in subclasses for content-specific readiness."""
        return

    async def _new_page(self, p: Playwright, attempts: int = 3) -> Page:
        """Connect to the UC browser and open a new tab."""
        for attempt in range(attempts):
            if HeadedBrowser._browser is None:
                raise RuntimeError(f"Headed Browser not connected for integration: {self.name}")

            try:
                endpoint_url = HeadedBrowser._browser.get_endpoint_url()
                browser = await p.chromium.connect_over_cdp(endpoint_url, timeout=10_000)
                context: BrowserContext = browser.contexts[0]
                await self._prepare_context(context)
                return await context.new_page()

            except PlaywrightError as e:
                if attempt < attempts - 1:
                    logger.warning(f"Connection attempt {attempt + 1} failed, retrying...")
                    await self._reconnect()
                    continue
                raise RuntimeError(f"Failed to initiate a new browser page.") from e

            except Exception as e:
                raise RuntimeError(f"Failed to initiate a new browser page.") from e

    async def _connect_locked_internal(self):
        """Internal helper to connect while lock is already held."""
        if HeadedBrowser._browser is not None:
            return

        try:
            xvfb_metrics = "1920,1080" if sys.platform.startswith("linux") else None
            HeadedBrowser._browser = await cdp_driver.start_async(
                headless=False,
                uc=True,
                no_sandbox=True,
                disable_setuid_sandbox=True,
                start_maximized=True,
                xvfb_metrics=xvfb_metrics,
                timeout=30,
                chromium_arg="--ignore-certificate-errors",
            )
            if HeadedBrowser._browser:
                logger.debug("cdp_driver started successfully.")
        except Exception:
            logger.error(f"Failed to start/restart Headed Browser for integration: {self.name}", exc_info=True)
            self._cleanup_resources()

    async def _get(self, url: str, **kwargs) -> MultimodalSequence:
        # Fresh Playwright/CDP session per request. Reusing one connection across requests
        # deadlocks on the second URL (CDP session wedges after the first page lifecycle).
        async with async_playwright() as p:
            page = await self._new_page(p)
            try:
                await page.set_viewport_size({"width": 1920, "height": 1080})
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                await self._settle_after_goto(page)

                if target := await self._extract_content(page):
                    html, source = await self._html_and_source(target, page)
                    if html:
                        from scrapemm.util import to_multimodal_sequence
                        return await to_multimodal_sequence(
                            html, session=page.context.request, url=url, source_element=source
                        )
            finally:
                await page.close()

        raise RetrievalFailed(f"{self.name} integration was unable to extract content from {url}.")

    @staticmethod
    async def _html_and_source(
            target: ContentTarget, page: Page
    ) -> tuple[Optional[str], Page | Frame]:
        """Resolve HTML and a Frame/Page suitable for in-page media fetch."""
        if isinstance(target, ElementHandle):
            html = await target.evaluate("el => el.outerHTML")
            source = await target.owner_frame() or page
            return html, source
        return await target.content(), target

    def _cleanup_resources(self):
        """Close the shared UC browser."""
        if HeadedBrowser._browser:
            try:
                HeadedBrowser._browser.quit()
            except Exception:
                logger.debug("Error while quitting headed browser", exc_info=True)
            HeadedBrowser._browser = None
        self.connected = False

    async def _extract_content(self, page: Page) -> Optional[ContentTarget]:
        """Change this function as needed to make it work for specific platforms.
        Returns the page, frame, or element expected to contain the content."""
        return page
