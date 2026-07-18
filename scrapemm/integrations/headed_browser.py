import logging
import sys
from typing import Optional

from ezmm import MultimodalSequence
from playwright.async_api import (
    async_playwright, Page, Frame, ElementHandle, Playwright, Browser as PlaywrightBrowser,
)
from seleniumbase import cdp_driver
from seleniumbase.undetected.cdp_driver.browser import Browser

from scrapemm.integrations.base import RetrievalIntegration

logger = logging.getLogger("scrapeMM")

ContentTarget = Page | Frame | ElementHandle


class HeadedBrowser(RetrievalIntegration):
    """Base class for retrieval integrations that need a headed browser to avoid bot blocking
     mechanisms (e.g., Cloudflare) when retrieving web content.
    Serves itself as a generic retrieval integration for platforms that don't need special handling."""
    name = "Headed Browser"
    domains = ["mvau.lt"]

    _browser: Optional[Browser] = None
    _playwright: Optional[Playwright] = None
    _pw_browser: Optional[PlaywrightBrowser] = None

    async def _connect(self):
        """Persistent connection is managed within the class instance."""
        if self._browser:
            await self._cleanup_resources()

        try:
            xvfb_metrics = "1920,1080" if sys.platform.startswith("linux") else None

            # Start cdp_driver (UC Mode)
            self._browser = await cdp_driver.start_async(
                headless=False,
                uc=True,
                no_sandbox=True,
                disable_setuid_sandbox=True,
                start_maximized=True,
                xvfb_metrics=xvfb_metrics,
                timeout=30,
                chromium_arg="--ignore-certificate-errors",
            )
            if self._browser:
                logger.debug("cdp_driver started successfully.")
        except Exception as e:
            logger.error(f"Failed to start cdp_driver for Internet Archive integration: {e}", exc_info=True)
            await self._cleanup_resources()

        self.connected = True

    async def _ensure_playwright(self) -> PlaywrightBrowser:
        """Reuse a single Playwright/CDP connection across requests."""
        if self._pw_browser and self._pw_browser.is_connected():
            return self._pw_browser

        await self._close_playwright()
        self._playwright = await async_playwright().start()
        endpoint_url = self._browser.get_endpoint_url()
        self._pw_browser = await self._playwright.chromium.connect_over_cdp(endpoint_url, timeout=10_000)
        return self._pw_browser

    async def _close_playwright(self):
        if self._pw_browser:
            try:
                await self._pw_browser.close()
            except Exception:
                pass
            self._pw_browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    async def _settle_after_goto(self, page: Page) -> None:
        """Optional post-navigation settle. Override in subclasses for content-specific readiness."""
        # Default: no fixed sleep. Subclasses that need more should wait on concrete signals.
        return

    async def _get(self, url: str, **kwargs) -> Optional[MultimodalSequence]:
        # Fetch HTML content from the given URL using Playwright.
        browser = await self._ensure_playwright()
        context = browser.contexts[0]
        page = await context.new_page()

        try:
            await page.set_viewport_size({"width": 1920, "height": 1080})

            # domcontentloaded: return as soon as the DOM is parseable. Waiting for "load"
            # often burns many seconds on archive/analytics assets after content is ready.
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            await self._settle_after_goto(page)

            # Extract the element that contains the content we're interested in
            if target := await self._extract_content(page):
                html, source = await self._html_and_source(target, page)
                if html:
                    from scrapemm.util import to_multimodal_sequence
                    return await to_multimodal_sequence(
                        html, session=page.context.request, url=url, source_element=source
                    )
        finally:
            await page.close()

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

    async def _cleanup_resources(self):
        """Close the Playwright connection and UC browser."""
        await self._close_playwright()
        if self._browser:
            try:
                self._browser.quit()
            except Exception:
                pass
            self._browser = None
            self.connected = False

    async def _extract_content(self, page: Page) -> Optional[ContentTarget]:
        """Change this function as needed to make it work for specific platforms.
        Returns the page, frame, or element expected to contain the content."""
        return page
