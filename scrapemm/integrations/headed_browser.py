import logging
import sys
from typing import Optional

from ezmm import MultimodalSequence
from playwright.async_api import async_playwright, Page, Frame, ElementHandle
from seleniumbase import cdp_driver
from seleniumbase.undetected.cdp_driver.browser import Browser

from scrapemm import RetrievalFailed
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

    async def _connect(self):
        """Persistent UC browser; Playwright connects over CDP per request."""
        if self._browser:
            self._cleanup_resources()

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
            self.connected = True
        except Exception:
            logger.error(f"Failed to start Headed Browser for integration: {self.name}", exc_info=True)
            self._cleanup_resources()
            self.connected = False

    async def _prepare_context(self, context) -> None:
        """Optional hook before a new page is created (e.g. inject cookies)."""
        return

    async def _settle_after_goto(self, page: Page) -> None:
        """Optional post-navigation settle. Override in subclasses for content-specific readiness."""
        # Default: no fixed sleep. Subclasses that need more should wait on concrete signals.
        return

    async def _get(self, url: str, **kwargs) -> Optional[MultimodalSequence]:
        # Fresh Playwright/CDP session per request. Reusing one connection across requests
        # deadlocks on the second URL (CDP session wedges after the first page lifecycle).
        async with async_playwright() as p:
            endpoint_url = self._browser.get_endpoint_url()
            browser = await p.chromium.connect_over_cdp(endpoint_url, timeout=10_000)
            context = browser.contexts[0]
            page = await context.new_page()

            try:
                # After new_page: more reliable for CDP/UC contexts than preparing beforehand.
                await self._prepare_context(context)
                await page.set_viewport_size({"width": 1920, "height": 1080})

                # domcontentloaded: return as soon as the DOM is parseable. Waiting for "load"
                # often burns many seconds on archive/analytics assets after content is ready.
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
        """Close the UC browser."""
        if self._browser:
            try:
                self._browser.quit()
            except Exception:
                logger.debug("Error while quitting headed browser", exc_info=True)
            self._browser = None
            self.connected = False

    async def _extract_content(self, page: Page) -> Optional[ContentTarget]:
        """Change this function as needed to make it work for specific platforms.
        Returns the page, frame, or element expected to contain the content."""
        return page
