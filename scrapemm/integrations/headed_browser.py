import asyncio
import logging
import sys
from typing import Optional, ClassVar

from playwright.async_api import async_playwright, Page, Frame, ElementHandle, Playwright, \
    BrowserContext, Error as PlaywrightError
from playwright._impl._errors import TargetClosedError
from seleniumbase import cdp_driver
from seleniumbase.undetected.cdp_driver.browser import Browser

from scrapemm import RetrievalFailed
from scrapemm.common.retrieval_integration import RetrievalIntegration
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.util import get_domain

logger = logging.getLogger("scrapeMM")

ContentTarget = Page | Frame | ElementHandle

# Substrings indicating the shared browser process itself died (not just a page-level
# issue), seen in Playwright error messages when the underlying Chrome process crashes
# or the CDP connection is severed.
_BROWSER_CRASH_MARKERS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "browser has disconnected",
    "connection closed",
    "websocket error",
    "econnrefused",
)


class HeadedBrowser(RetrievalIntegration):
    """Base class for retrieval integrations that need a headed browser to avoid bot blocking
     mechanisms (e.g., Cloudflare) when retrieving web content.
    Serves itself as a generic retrieval integration for platforms that don't need special handling."""
    name = "Headed Browser"
    domains = ["mvau.lt"]

    # Shared UC browser for all HeadedBrowser integrations (Perma.cc, Archive.org, …).
    _browser: ClassVar[Optional[Browser]] = None
    # Increments every time the shared browser is (re)started. Used to coordinate crash
    # recovery across concurrent tasks so only one of them actually restarts it.
    _generation: ClassVar[int] = 0
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    async def get(self, url: str, **kwargs) -> ScrapedContent:
        """Executes the retrieval routine, bypassing the generic connected-cache from
        `RetrievalIntegration.get()`. A shared-browser startup failure is transient (e.g.
        under heavy concurrent load) rather than a permanent misconfiguration, so it must
        not permanently disable this integration for the rest of the process the way a
        missing API credential would. `_new_page()`/`_ensure_browser()` already retry and
        self-heal the shared browser on every call, and `_get()` never returns None —
        it either succeeds or raises an informative exception."""
        assert get_domain(url) in self.domains, f"Invalid domain {get_domain(url)} for integration {self.name}."
        logger.debug(f"Calling {self.name} service for {url}")
        return await self._get(url, **kwargs)

    async def _connect(self):
        """Establishes a connection to a persistent, shared UC browser. Playwright connects
        over CDP per request. Not used to gate `get()` (see override above); kept so the
        shared browser can be pre-warmed explicitly if desired."""
        browser, _ = await self._ensure_browser()
        self.connected = browser is not None

    async def _ensure_browser(self, bad_generation: Optional[int] = None) -> tuple[Optional[Browser], int]:
        """Returns a live shared browser and its generation number, starting or restarting
        it as needed.

        If `bad_generation` matches the *current* generation, the caller is the one who
        detected that exact browser instance is dead, so it restarts it. If another task
        already replaced the browser in the meantime (generation advanced since the caller
        last looked), the existing fresh browser is returned as-is — no redundant restart.
        This makes crash recovery "single-flight": no matter how many concurrent tasks hit
        the same dead browser, only one of them actually kills and restarts it.
        """
        async with HeadedBrowser._lock:
            stale = HeadedBrowser._browser is None or HeadedBrowser._browser.stopped
            superseded = bad_generation is not None and bad_generation == HeadedBrowser._generation
            if stale or superseded:
                self._cleanup_resources()
                await self._start_browser_locked()
                HeadedBrowser._generation += 1
            return HeadedBrowser._browser, HeadedBrowser._generation

    async def _start_browser_locked(self):
        """Starts the shared UC browser. Caller must already hold `_lock`."""
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

    async def _prepare_context(self, context: BrowserContext) -> None:
        """Optional hook before a new page is created (e.g. inject cookies)."""
        return

    async def _settle_after_goto(self, page: Page) -> None:
        """Optional post-navigation settle. Override in subclasses for content-specific readiness."""
        return

    async def _new_page(self, p: Playwright, attempts: int = 3) -> tuple[Page, int]:
        """Connect to the UC browser and open a new tab. Returns the page along with the
        browser generation it was opened on, so callers can report a crash precisely."""
        bad_generation = None
        for attempt in range(attempts):
            browser, generation = await self._ensure_browser(bad_generation)
            if browser is None:
                if attempt < attempts - 1:
                    logger.debug(f"Shared browser unavailable, attempt {attempt + 1} failed, retrying...")
                    continue
                raise RuntimeError(f"Headed Browser not connected.")

            try:
                connection = await p.chromium.connect_over_cdp(browser.get_endpoint_url(), timeout=10_000)
                context: BrowserContext = connection.contexts[0]
                await self._prepare_context(context)
                return await context.new_page(), generation

            except PlaywrightError as e:
                if attempt < attempts - 1:
                    logger.debug(f"Connection attempt {attempt + 1} failed, recovering shared browser...")
                    bad_generation = generation
                    continue
                raise RuntimeError(f"Failed to initiate a new browser page.") from e

            except Exception as e:
                raise RuntimeError(f"Failed to initiate a new browser page.") from e

        raise RuntimeError(f"Failed to initiate a new browser page after {attempts} attempts.")

    @staticmethod
    def _is_browser_crash(exc: BaseException) -> bool:
        """True if the exception indicates the shared browser process itself died,
        as opposed to a page-level issue (timeout, navigation error, etc.)."""
        if not isinstance(exc, PlaywrightError):
            return False
        if isinstance(exc, TargetClosedError):
            return True
        message = str(exc).lower()
        return any(marker in message for marker in _BROWSER_CRASH_MARKERS)

    @staticmethod
    def _is_client_redirect_abort(exc: BaseException) -> bool:
        """True if `page.goto()` failed with net::ERR_ABORTED, which Chromium raises when
        the page itself starts a second navigation (e.g. a JS/meta-refresh redirect) before
        our goto's `wait_until` condition was reached — the original request gets cancelled
        in favor of the redirect. Very common on Wayback Machine snapshots of SPAs (e.g.
        X/Twitter), which client-side-redirect almost immediately after the initial HTML
        arrives. The page itself is fine; only the specific `goto()` call was raced out."""
        return isinstance(exc, PlaywrightError) and "err_aborted" in str(exc).lower()

    async def _get(self, url: str, **kwargs) -> ScrapedContent:
        # Fresh Playwright/CDP session per request. Reusing one connection across requests
        # deadlocks on the second URL (CDP session wedges after the first page lifecycle).
        for attempt in range(2):  # one try + one crash-triggered retry
            async with async_playwright() as p:
                page, generation = await self._new_page(p)
                try:
                    await page.set_viewport_size({"width": 1920, "height": 1080})

                    # domcontentloaded: return as soon as the DOM is parseable. Waiting for "load"
                    # often burns many seconds on archive/analytics assets after content is ready.
                    try:
                        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                    except PlaywrightError as e:
                        if not self._is_client_redirect_abort(e):
                            raise
                        # The page already redirected itself; give the new document a moment
                        # to settle instead of failing the whole retrieval over a benign race.
                        logger.debug(f"Navigation to {url} was superseded by a client-side "
                                     f"redirect (net::ERR_ABORTED); continuing on {page.url}.")
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=30000)
                        except Exception:
                            pass
                    await self._settle_after_goto(page)

                    if target := await self._extract_content(page):
                        html, source = await self._html_and_source(target, page)
                        if html:
                            # Media must be resolved while the page is still open
                            from scrapemm.util import to_scraped_content
                            return await to_scraped_content(
                                html, session=page.context.request,
                                output_format=kwargs.get("output_format", "multimodal"),
                                url=url, source_element=source
                            )
                    break  # No content found — not a crash, don't retry.

                except PlaywrightError as e:
                    if attempt == 0 and self._is_browser_crash(e):
                        logger.warning(
                            f"Headed Browser crashed while retrieving {url} with {self.name}; "
                            f"recovering and retrying once."
                        )
                        # Trigger (or await an already in-flight) single-flight recovery before retrying.
                        await self._ensure_browser(generation)
                        continue
                    raise
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass

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
        """Close the shared UC browser. Caller must hold `_lock` if racing with `_ensure_browser`."""
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
