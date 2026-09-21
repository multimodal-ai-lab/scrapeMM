import asyncio
import atexit
import json
import logging
import os
import sys
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import Optional, ClassVar
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, Frame, ElementHandle, Playwright, \
    BrowserContext, Error as PlaywrightError
from playwright._impl._errors import TargetClosedError
from seleniumbase import cdp_driver
from seleniumbase.undetected.cdp_driver.browser import Browser

from scrapemm import RetrievalFailed
from scrapemm.common import get_config_var
from scrapemm.common.paths import BROWSER_PROFILE_PATH
from scrapemm.common.retrieval_integration import RetrievalIntegration
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.util import get_domain

logger = logging.getLogger("scrapeMM")

ContentTarget = Page | Frame | ElementHandle

# Local port through which the browser window is reached when scrapeMM runs on a machine
# without a screen, see `remote_view_hint()`. Override with
# `update_config(devtools_local_port=...)` if that port is taken on your machine.
DEFAULT_DEVTOOLS_LOCAL_PORT = 9222


def _devtools_local_port() -> int:
    return int(get_config_var("devtools_local_port") or DEFAULT_DEVTOOLS_LOCAL_PORT)


def _browser_args() -> list[str]:
    """Flags for the shared browser.

    Archives frequently serve snapshots with expired or mismatching certificates, which
    must not stop the retrieval. The origin allowlist lets the DevTools frontend attach
    through an SSH tunnel: since M111, Chrome answers DevTools WebSocket handshakes whose
    Origin is not allowlisted with 403, which the frontend reports as "WebSocket
    disconnected". Automation is unaffected (it sends no Origin at all), so only the
    single origin the frontend is served from is allowed, not every origin.
    """
    return [
        "--ignore-certificate-errors",
        f"--remote-allow-origins=http://localhost:{_devtools_local_port()}",
        # A reused profile remembers how the last run ended. After an unclean exit
        # Chromium greets the next start with its crash-restore bubble and reopens the
        # previous tabs, which stalls the CDP attach long enough to time out. None of
        # that is wanted for automation, so it is switched off.
        "--hide-crash-restore-bubble",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
    ]


async def _close_browser_gracefully(browser: Browser, settle: float = 2.0) -> bool:
    """Asks Chromium to shut itself down via CDP, so it flushes its profile to disk.

    Returns whether the request got through. Best-effort throughout: a browser that
    already died is closed by definition, and a failure here only costs persistence,
    never the retrieval.
    """
    try:
        async with async_playwright() as p:
            connection = await p.chromium.connect_over_cdp(
                browser.get_endpoint_url(), timeout=5_000)
            context = connection.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            session = await context.new_cdp_session(page)
            # Browser.close tears down the connection it arrives on, so both of these
            # are expected to raise once the browser is actually gone.
            with suppress(Exception):
                await session.send("Browser.close")
            with suppress(Exception):
                await connection.close()
    except Exception:
        logger.debug("Could not close the headed browser gracefully; "
                     "its profile may miss the most recent changes.", exc_info=True)
        return False

    # Give the process a moment to finish writing before it is terminated
    await asyncio.sleep(settle)
    return True


# Exclusive handle on the profile, held for as long as this process runs. The OS drops
# it when the process ends, so a crash cannot leave a stale lock behind.
_profile_lock: Optional[object] = None


def _acquire_profile_lock(path: Path) -> bool:
    """Takes an exclusive lock on the profile directory, so only one process uses it.

    Chromium refuses to run two instances on one profile -- but it fails silently and
    unhelpfully: the second launch hands its request to the already running instance,
    which pops up an empty window, and then exits. The caller is left waiting on a
    browser it does not control. So the clash has to be detected before launching.
    """
    global _profile_lock
    if _profile_lock is not None:
        return True  # This process already holds it

    try:
        handle = open(path / "scrapemm.lock", "a+b")
    except OSError:
        return False

    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False

    _profile_lock = handle
    return True


def _resolve_profile_dir() -> Optional[str]:
    """Returns the directory of the shared browser's persistent profile, creating it if
    needed. Returns None (i.e. a throwaway profile) if the profile is unusable, already
    taken by another process, or disabled via `update_config(browser_profile=False)`."""
    configured = get_config_var("browser_profile", True)
    if configured is False:
        return None

    path = Path(configured) if isinstance(configured, str) else BROWSER_PROFILE_PATH
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning(f"Could not create the browser profile directory {path}.", exc_info=True)
        return None

    if not _acquire_profile_lock(path):
        logger.warning(
            f"The browser profile at {path} is in use by another scrapeMM process. "
            f"Running on a throwaway profile instead, so sessions established now will "
            f"not persist. Close the other process if you are capturing a session."
        )
        return None

    return str(path)


def _resolve_browser_executable(playwright: Optional[Playwright]) -> Optional[str]:
    """Returns the path of the browser binary to run the shared browser with. Prefers
    Playwright's bundled Chromium over any locally installed Chrome because it is pinned:
    the browser version is then the same on every machine, and it is a build the scraped
    sites already know. Bot detection tends to distrust the newest Chrome release —
    Archive.today, for instance, answers Chrome 153 with a decoy page (nginx' default
    page) while serving the bundled Chromium normally.

    Returns None if no specific binary could be determined, leaving the choice to
    SeleniumBase (which picks the locally installed Chrome).
    """
    if configured := get_config_var("browser_executable_path"):
        return configured

    if playwright is None:
        return None

    try:
        return playwright.chromium.executable_path
    except Exception:
        logger.debug("Could not locate Playwright's bundled Chromium. Falling back to "
                     "the locally installed browser.", exc_info=True)
        return None


async def remote_view_hint(page: Page) -> Optional[str]:
    """Returns instructions for watching and clicking the given page from another
    machine. Needed when scrapeMM runs on a headless server but a human has to interact
    with the browser, e.g. to pass an access check. The browser has to stay where it is:
    anti-bot clearances are bound to the browser and the IP address that earned them.

    Chrome serves its own DevTools frontend on the debugging port and accepts connections
    whose Host header is localhost, which an SSH tunnel satisfies — so nothing needs to be
    installed on the server.
    """
    browser = HeadedBrowser._browser
    if browser is None:
        return None

    try:
        port = urlparse(browser.get_endpoint_url()).port
        target_id = await asyncio.to_thread(_devtools_target_id, port, page.url)
    except Exception:
        logger.debug("Could not determine the browser's debugging endpoint.", exc_info=True)
        return None

    if not target_id:
        return None

    local_port = _devtools_local_port()
    hint = (f"🖥 No screen on this machine? Reach the browser window from your local one:\n"
            f"   1. Locally:  ssh -N -L {local_port}:127.0.0.1:{port} <user>@<this host>\n"
            f"   2. Open   :  http://localhost:{local_port}/devtools/inspector.html"
            f"?ws=localhost:{local_port}/devtools/page/{target_id}\n"
            f"   The page renders there and your clicks reach it. Keep the local port: the "
            f"browser only accepts the DevTools connection from that exact origin.")
    if display := os.environ.get("DISPLAY"):
        hint += (f"\n   Alternative with plain X input (needs x11vnc on this machine): "
                 f"x11vnc -display {display} -localhost, then tunnel port 5900.")
    return hint


def _devtools_target_id(port: int, page_url: str) -> Optional[str]:
    """Looks up the debugging target id of the page showing `page_url`."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        targets = json.load(response)
    for target in targets:
        if target.get("type") == "page" and target.get("url") == page_url:
            return target.get("id")
    return None


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


@atexit.register
def _persist_browser_profile_at_exit() -> None:
    """Closes the shared browser cleanly when the process ends.

    Without this, the ordinary case -- start scrapeMM, retrieve, exit -- would discard
    every session refresh the browser collected along the way, because the profile is
    only written on a flush timer or a clean shutdown.
    """
    browser = HeadedBrowser._browser
    if browser is None:
        return
    try:
        asyncio.run(_close_browser_gracefully(browser))
    except Exception:
        pass  # Interpreter shutdown is no place to raise


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
        async with async_playwright() as p:
            browser, _ = await self._ensure_browser(playwright=p)
        self.connected = browser is not None

    async def _ensure_browser(self, bad_generation: Optional[int] = None,
                              playwright: Optional[Playwright] = None) -> tuple[Optional[Browser], int]:
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
                if superseded and not stale:
                    # The browser is being replaced while still alive, so let it persist
                    # its profile first. A crashed one has nothing left to flush.
                    await self._shutdown_browser()
                else:
                    self._cleanup_resources()
                await self._start_browser_locked(playwright)
                HeadedBrowser._generation += 1
            return HeadedBrowser._browser, HeadedBrowser._generation

    async def _start_browser_locked(self, playwright: Optional[Playwright] = None):
        """Starts the shared UC browser. Caller must already hold `_lock`.

        Runs on a persistent profile so that sessions established in this browser (see
        `ArchiveToday.capture_session()`) survive a restart. If that profile cannot be
        used -- it is corrupted, or another scrapeMM process already holds it, since
        Chromium allows only one instance per profile -- the browser is started on a
        throwaway profile instead: retrieving without persistence beats not retrieving.
        """
        xvfb_metrics = "1920,1080" if sys.platform.startswith("linux") else None
        executable_path = _resolve_browser_executable(playwright)
        user_agent = get_config_var("browser_user_agent")

        for profile in (_resolve_profile_dir(), None):
            try:
                logger.debug(f"Starting headed browser: {executable_path or 'system default'} "
                             f"(profile: {profile or 'throwaway'})")
                HeadedBrowser._browser = await cdp_driver.start_async(
                    headless=False,
                    uc=True,
                    no_sandbox=True,
                    disable_setuid_sandbox=True,
                    start_maximized=True,
                    xvfb_metrics=xvfb_metrics,
                    timeout=30,
                    # Note: `cdp_driver.start_async()` has no 'chromium_arg' parameter. Passing
                    # browser flags any other way makes them end up in **kwargs, where they are
                    # silently dropped.
                    browser_args=_browser_args(),
                    browser_executable_path=executable_path,
                    user_data_dir=profile,
                    # Bot checks bind their clearance to the exact user agent that earned
                    # it, so a browser update would silently invalidate every stored
                    # session. Pinning the agent recorded at capture time prevents that.
                    agent=user_agent,
                )
                if HeadedBrowser._browser:
                    logger.debug("cdp_driver started successfully.")
                    return
            except Exception:
                self._cleanup_resources()
                if profile is not None:
                    logger.warning(
                        f"Could not start the browser on its persistent profile ({profile}). "
                        f"Falling back to a throwaway profile, so sessions will not persist. "
                        f"This is expected if another scrapeMM process is already running.",
                        exc_info=True,
                    )
                    continue
                logger.error(f"Failed to start/restart Headed Browser for integration: "
                             f"{self.name}", exc_info=True)

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
            browser, generation = await self._ensure_browser(bad_generation, playwright=p)
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
        """Close the shared UC browser. Caller must hold `_lock` if racing with `_ensure_browser`.

        Prefer `_shutdown_browser()` where awaiting is possible: this one terminates the
        browser without letting it flush its profile.
        """
        if HeadedBrowser._browser:
            try:
                HeadedBrowser._browser.quit()
            except Exception:
                logger.debug("Error while quitting headed browser", exc_info=True)
            HeadedBrowser._browser = None
        self.connected = False

    async def _shutdown_browser(self):
        """Closes the shared browser, giving it the chance to persist its profile first.

        Chromium writes cookies to disk on a timer and on a clean shutdown; killing the
        process in between discards everything written since the last flush. That is fatal
        here, because the tokens a bot check hands out get refreshed on every response --
        killing the browser would throw away exactly the fresh session we want to keep.
        """
        browser = HeadedBrowser._browser
        if browser is not None:
            await _close_browser_gracefully(browser)
        self._cleanup_resources()

    async def _extract_content(self, page: Page) -> Optional[ContentTarget]:
        """Change this function as needed to make it work for specific platforms.
        Returns the page, frame, or element expected to contain the content."""
        return page
