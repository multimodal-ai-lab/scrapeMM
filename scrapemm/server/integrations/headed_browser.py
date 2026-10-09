import asyncio
import base64
import atexit
import json
import logging
import os
import socket
import sys
import tempfile
import time
import urllib.request
import uuid
from contextlib import suppress, contextmanager
from contextvars import ContextVar

import aiohttp
from pathlib import Path
from typing import Optional, ClassVar
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, Frame, ElementHandle, Playwright, \
    BrowserContext, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from playwright._impl._errors import TargetClosedError
from seleniumbase import cdp_driver
from seleniumbase.undetected.cdp_driver.browser import Browser

from scrapemm.common import RetrievalFailed
from scrapemm.common.exceptions import TargetUnavailableError, RegionBlockedError
from scrapemm.server.download.documents import document_extension
from scrapemm.server import screenshot, timing
from scrapemm.server.config import get_config_var
from scrapemm.server.download.browser import BrowserMedia, annotate_rendered_media
from scrapemm.server.paths import BROWSER_PROFILE_PATH
from scrapemm.server import budget
from scrapemm.server.reachability import is_network_failure
from scrapemm.server.integrations.base import RetrievalIntegration
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.server.util import get_domain

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
        # The HTTP cache, bounded: archive replays and media pages fill it fast
        f"--disk-cache-size={DISK_CACHE_BYTES}",
        *_fill_screen_args(),
    ]


def _fill_screen_args() -> list[str]:
    """Makes the window fill the container's virtual screen. Xvfb runs without a window
    manager, so --start-maximized does nothing there, and the window kept SeleniumBase's
    default of 1280x840 at (20, 54): the CAPTCHA panel showed it small, in a black
    frame. Later flags win, so these override SeleniumBase's own."""
    size = os.environ.get("SCRAPEMM_SCREEN_SIZE", "")  # E.g. "1440x900x24"
    try:
        width, height = (int(n) for n in size.split("x")[:2])
    except ValueError:
        return []
    return ["--window-position=0,0", f"--window-size={width},{height}"]


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


def _clear_stale_chromium_lock(path: Path) -> None:
    """Removes Chromium's own profile lock if nobody holds it any more.

    Chromium records its lock as a symlink to "<hostname>-<pid>". A recreated container
    has a new hostname, so the lock the previous container left in the (persistent)
    profile looks like another computer using the profile -- and Chromium refuses to
    start on it, which cost every stored session. Holding `scrapemm.lock` already rules
    out another scrapeMM process, so a lock of another host or of a dead process is
    stale. A lock of a Chromium still running here is left alone.
    """
    lock = path / "SingletonLock"
    try:
        target = os.readlink(lock)
    except OSError:
        return  # No lock (or not a symlink, as on Windows): nothing to do
    host, _, pid = target.rpartition("-")
    if host == socket.gethostname() and pid.isdigit() and _process_alive(int(pid)):
        return
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (path / name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug(f"Could not remove the stale {name} in {path}.", exc_info=True)
    logger.info(f"Removed a stale browser profile lock left by {target}.")


def _forget_open_tabs(path: Path) -> None:
    """Deletes the profile's record of its open tabs, so a restarted browser does not
    reopen them. After an unclean exit (a container restart) Chromium restored every tab
    the last run had open, ads and all, and each new connection then attached to them.
    Cookies and storage, i.e. the sessions worth keeping, live elsewhere."""
    import shutil
    with suppress(OSError):
        shutil.rmtree(path / "Default" / "Sessions")


# --- Bounded storage of archive replays --------------------------------------------------
# ReplayWeb.page (Perma.cc's rejouer.perma.cc, Ghostarchive) keeps every archive it
# replays in its origin's IndexedDB and never lets go of it: on the production server,
# rejouer.perma.cc's had grown to 50 GB of a 52 GB profile. A replay fetches its archive
# again when the store is gone, and the scrapeMM cache keeps finished results for good,
# so emptying it costs one slower first retrieval per capture.
DISK_CACHE_BYTES = 1024 ** 3
REPLAY_ORIGINS = ("https://rejouer.perma.cc", "https://ghostarchive.org", "https://replayweb.page")
# An origin storing more than this in IndexedDB is emptied too, replay or not
ORIGIN_STORAGE_CAP = 2 * 1024 ** 3
# How often the running browser's replay storage is looked at, see `_watch_replay_storage()`
STORAGE_CHECK_INTERVAL = 45 * 60
# The pages through which each replay origin is used (its frames sit inside them)
_REPLAY_PAGE_HOSTS = {"https://rejouer.perma.cc": ("perma.cc", "rejouer.perma.cc"),
                      "https://ghostarchive.org": ("ghostarchive.org",),
                      "https://replayweb.page": ("replayweb.page",)}


def _indexeddb_entries(profile: Path, origin: str) -> list[Path]:
    """The exact IndexedDB paths of an origin in a profile: Chromium names them after
    the origin ("https_rejouer.perma.cc_0", with ".indexeddb.leveldb" and
    ".indexeddb.blob" in older layouts)."""
    scheme, host = origin.split("://", 1)
    base = profile / "Default" / "IndexedDB" / f"{scheme}_{host}_0"
    return [base, base.with_name(base.name + ".indexeddb.leveldb"),
            base.with_name(base.name + ".indexeddb.blob")]


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with suppress(OSError):
                total += os.path.getsize(os.path.join(root, name))
    return total


def _gb(size: int) -> str:
    return f"{size / 1024 ** 3:.1f} GB" if size >= 1024 ** 3 else f"{size / 1024 ** 2:.0f} MB"


def _forget_replay_storage(profile: Path) -> None:
    """Deletes, before the browser opens the profile, the IndexedDB of the replay origins
    and of any origin above `ORIGIN_STORAGE_CAP`. Exactly those directories: cookies,
    logins and every other origin's storage stay."""
    import shutil
    directory = profile / "Default" / "IndexedDB"
    if not directory.is_dir():
        return
    doomed = [entry for origin in REPLAY_ORIGINS for entry in _indexeddb_entries(profile, origin)
              if entry.exists()]
    with suppress(OSError):
        for entry in directory.iterdir():
            if entry not in doomed and entry.is_dir() and _size(entry) > ORIGIN_STORAGE_CAP:
                doomed.append(entry)
    freed = 0
    for entry in doomed:
        size = _size(entry)
        try:
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
            freed += size
        except OSError:
            logger.warning(f"Could not delete the browser storage {entry}.", exc_info=True)
    if freed:
        logger.info(f"Freed {_gb(freed)} of archive replay storage in the "
                    f"browser profile ({', '.join(e.name for e in doomed)}).")


async def _clear_origin_storage(browser: Browser, origin: str) -> None:
    """Empties an origin's IndexedDB, CacheStorage and service workers in the running
    browser (CDP Storage.clearDataForOrigin), so it never finds files deleted under it.
    The browser target has no storage partition to clear (it answers "Internal error"),
    so the call goes through a blank tab opened for it."""
    port = urlparse(browser.get_endpoint_url()).port
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}/json/version",
                               timeout=aiohttp.ClientTimeout(total=5)) as response:
            ws_url = (await response.json(content_type=None))["webSocketDebuggerUrl"]
        async with session.ws_connect(ws_url, max_msg_size=0) as ws:
            ids = iter(range(1, 1000))

            async def call(method: str, params: dict, session_id: str = None) -> dict:
                message_id = next(ids)
                await ws.send_json({"id": message_id, "method": method, "params": params,
                                    **({"sessionId": session_id} if session_id else {})})
                async for message in ws:
                    data = message.json()
                    if data.get("id") == message_id:
                        if "error" in data:
                            raise RuntimeError(f"{method}: {data['error'].get('message')}")
                        return data.get("result", {})
                raise ConnectionError("The browser closed the DevTools connection.")

            async with asyncio.timeout(60):
                target = (await call("Target.createTarget",
                                     {"url": "about:blank", "background": True}))["targetId"]
                try:
                    attached = await call("Target.attachToTarget",
                                          {"targetId": target, "flatten": True})
                    await call("Storage.clearDataForOrigin", {
                        "origin": origin,
                        "storageTypes": "indexeddb,cache_storage,service_workers",
                    }, attached["sessionId"])
                finally:
                    with suppress(Exception):
                        await call("Target.closeTarget", {"targetId": target})


async def _origin_in_use(browser: Browser, origin: str) -> bool:
    """Whether a tab shows a page through which the origin is used (a replay running)."""
    port = urlparse(browser.get_endpoint_url()).port
    hosts = _REPLAY_PAGE_HOSTS.get(origin, (urlparse(origin).hostname,))
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}/json/list",
                               timeout=aiohttp.ClientTimeout(total=5)) as response:
            targets = await response.json(content_type=None)
    # Pages only: the origin's service worker is a target of its own, and stays listed
    # long after the last replay closed
    return any(t.get("type") == "page"
               and (urlparse(t.get("url", "")).hostname or "").removeprefix("www.") in hosts
               for t in targets)


async def _watch_replay_storage() -> None:
    """While the server runs: every `STORAGE_CHECK_INTERVAL`, empties the storage of each
    replay origin above `ORIGIN_STORAGE_CAP`, once no tab uses it."""
    while True:
        await asyncio.sleep(STORAGE_CHECK_INTERVAL)
        with suppress(Exception):
            await check_replay_storage()


async def check_replay_storage() -> dict[str, int]:
    """One round of `_watch_replay_storage()`. Returns the bytes freed per origin."""
    browser, profile = HeadedBrowser._browser, _resolve_profile_dir()
    freed: dict[str, int] = {}
    if browser is None or browser.stopped or not profile:
        return freed
    for origin in REPLAY_ORIGINS:
        entries = _indexeddb_entries(Path(profile), origin)
        size = sum(await asyncio.gather(*(asyncio.to_thread(_size, e) for e in entries
                                          if e.exists())))
        if size <= ORIGIN_STORAGE_CAP:
            continue
        try:
            if await _origin_in_use(browser, origin):
                logger.info(f"{origin} stores {_gb(size)} in the browser; a "
                            f"replay is open, so it is emptied at the next check.")
                continue
            await _clear_origin_storage(browser, origin)
        except Exception as e:
            logger.warning(f"Could not empty the browser storage of {origin}: "
                           f"{type(e).__name__}: {e}")
            continue
        after = sum(await asyncio.gather(*(asyncio.to_thread(_size, e) for e in entries
                                           if e.exists())))
        freed[origin] = size - after
        logger.info(f"Emptied the browser storage of {origin}: {_gb(size)}, "
                    f"{_gb(size - after)} freed.")
    return freed


_storage_watch: Optional[asyncio.Task] = None


def _start_storage_watch() -> None:
    global _storage_watch
    if _storage_watch is None or _storage_watch.done():
        _storage_watch = asyncio.ensure_future(_watch_replay_storage())


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Exists, just not ours to signal
    except OSError:
        return False
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
    _clear_stale_chromium_lock(path)

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


# Seconds a tab gets to answer before it counts as unresponsive (see _close_orphaned_tabs)
TAB_ANSWER_TIMEOUT = 3
# Longest a single browser retrieval may take. Beyond this one is stuck, and freeing its
# slot matters more than waiting for it -- it otherwise stalls its whole site's queue.
# What is slow to arrive (a video in an archive's replay) gives up before this, so that
# the page comes back without it instead of failing as a whole (see `budget`).
BROWSER_RETRIEVAL_TIMEOUT = 120

# The tabs that running retrievals opened (CDP target ids). Any other tab showing a
# page may be left over -- see `_close_orphaned_tabs()`.
_owned_tabs: set[str] = set()


async def _own(page: Page) -> None:
    try:
        session = await page.context.new_cdp_session(page)
        info = await session.send("Target.getTargetInfo")
        with suppress(Exception):
            await session.detach()
        page._scrapemm_target_id = info["targetInfo"]["targetId"]
        _owned_tabs.add(page._scrapemm_target_id)
    except Exception as e:
        # Harmless now (the sweep asks a tab before closing it), but worth seeing
        logger.info(f"Could not register a browser tab: {type(e).__name__}: {e}")


# --- The human's tab ------------------------------------------------------------------
# While somebody solves a CAPTCHA through the web UI's panel, the tab they see has to stay
# in front. Retrievals keep opening tabs meanwhile, and a tab opened the usual way
# (`context.new_page()`) becomes the window's foreground tab: it hid the CAPTCHA, even in
# the middle of a drag. So while a human tab is up, retrieval tabs open in the background
# (CDP `Target.createTarget` with `background`), and whatever else comes to the front, a
# popup say, is put behind it again. Background tabs render and run at full speed: the
# browser is started with SeleniumBase's --disable-background-timer-throttling,
# --disable-renderer-backgrounding and --disable-backgrounding-occluded-windows.
_opening_human_tab: ContextVar[bool] = ContextVar("scrapemm_opening_human_tab", default=False)
_human_page: Optional[Page] = None
_BACKGROUND_TAB_PREFIX = "about:blank#scrapemm-background-"
# Tasks started without anyone awaiting them, referenced so they are not collected
_background_tasks: set[asyncio.Task] = set()


@contextmanager
def human_tab():
    """Makes the tab that `HeadedBrowser._new_page()` opens within this block the one a
    human is looking at, and keeps it in front of all others until the block ends."""
    global _human_page
    token = _opening_human_tab.set(True)
    try:
        yield
    finally:
        _opening_human_tab.reset(token)
        _human_page = None


def _shown_human_tab() -> Optional[Page]:
    page = _human_page
    return page if page is not None and not page.is_closed() else None


def _run_soon(coroutine) -> None:
    task = asyncio.ensure_future(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _become_human_tab(page: Page) -> None:
    global _human_page
    _human_page = page
    context = page.context
    if not getattr(context, "_scrapemm_keeps_human_tab", False):
        context._scrapemm_keeps_human_tab = True
        context.on("page", _on_new_tab)
    _run_soon(_bring_to_front(page))


def _on_new_tab(page: Page) -> None:
    human = _shown_human_tab()
    if human is None or page is human or page.url.startswith(_BACKGROUND_TAB_PREFIX):
        return
    _run_soon(keep_in_front(human, settle=0.3))


async def keep_in_front(page: Page, settle: float = 0.0) -> None:
    """Brings the page to the front, unless it is there already. Needlessly bringing it
    there is not harmless: it activates the window, which ends a drag in progress (a
    slider CAPTCHA reset itself that way)."""
    if settle:
        await asyncio.sleep(settle)  # For the new tab to take the front first, if it does
    with suppress(Exception):
        if await asyncio.wait_for(page.evaluate("document.visibilityState"), 5) == "visible":
            return
    await _bring_to_front(page)


async def _bring_to_front(page: Page) -> None:
    with suppress(Exception):
        await asyncio.wait_for(page.bring_to_front(), timeout=10)


async def _open_background_tab(context: BrowserContext, timeout: float = 25) -> Page:
    """Opens a tab behind the one in front. Playwright cannot do that itself, so the tab
    is created over CDP, with a unique blank URL to recognise its page by."""
    marker = _BACKGROUND_TAB_PREFIX + uuid.uuid4().hex
    opened = asyncio.get_running_loop().create_future()

    def on_page(page: Page) -> None:
        if page.url == marker and not opened.done():
            opened.set_result(page)

    context.on("page", on_page)
    target_id = None
    try:
        session = await context.browser.new_browser_cdp_session()
        try:
            target_id = (await session.send("Target.createTarget",
                                            {"url": marker, "background": True}))["targetId"]
        finally:
            with suppress(Exception):
                await session.detach()
        for page in context.pages:  # In case the event came before the answer
            on_page(page)
        return await asyncio.wait_for(opened, timeout)
    except BaseException:
        if target_id is not None and not opened.done():
            _run_soon(_close_target(target_id))
        raise
    finally:
        context.remove_listener("page", on_page)


def renderer_crashed(page: Page) -> bool:
    """Whether the tab's renderer died (see `HeadedBrowser._browse()`)."""
    return getattr(page, "_scrapemm_crashed", False)


def release_page_soon(page: Page) -> None:
    """`release_page()` without waiting for it."""
    _run_soon(release_page(page))


async def release_page(page: Page) -> None:
    """Closes a tab opened with `HeadedBrowser._new_page()`. Bounded in time: closing
    must not hang a retrieval that is already done. If Playwright cannot close it in
    time (or the closing task is cancelled), the tab is closed through the browser's
    DevTools endpoint instead: a tab left open keeps running its scripts and ads for
    good -- dozens of them per batch once held 28 GB."""
    target_id = getattr(page, "_scrapemm_target_id", None)
    cancelled = False
    try:
        await asyncio.wait_for(page.close(), timeout=10)
    except asyncio.CancelledError:
        cancelled = True
    except Exception:
        pass
    try:
        closed = page.is_closed()
    except Exception:
        closed = False
    if not closed and target_id:
        await _close_target(target_id)
    _owned_tabs.discard(target_id)
    if cancelled:
        raise asyncio.CancelledError


async def _close_target(target_id: str, attempts: int = 3) -> bool:
    """Closes a tab by its target id via the DevTools HTTP endpoint, bypassing any
    Playwright connection. Returns whether the browser confirmed it."""
    browser = HeadedBrowser._browser
    if browser is None:
        return False
    try:
        port = urlparse(browser.get_endpoint_url()).port
    except Exception:
        return False
    for attempt in range(attempts):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/json/close/{target_id}",
                                       timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status == 200:
                        return True
                    if response.status == 404:
                        return True  # Gone already
        except Exception:
            pass
        await asyncio.sleep(1)
    logger.warning(f"Could not close the browser tab {target_id}; it stays open.")
    return False


def _close_late_tab(creating: asyncio.Future) -> None:
    """Closes a tab whose opening finished only after its retrieval gave up on it."""
    if creating.cancelled() or creating.exception() is not None:
        return
    page = creating.result()

    async def close():
        await _own(page)  # For the target id, should Playwright fail to close it
        await release_page(page)
    asyncio.ensure_future(close())


# The single Playwright connection all retrievals share, per event loop. Connecting is
# expensive and gets more so the more tabs are open: Playwright attaches to every tab,
# iframe and worker of the browser, and then receives the events of all of them. With a
# connection per retrieval, 32 concurrent retrievals each processed the traffic of all
# 32 pages -- the browser process sat at 250% CPU serving that, and single pages that
# take 3 s alone took 80 s in a batch. One connection attaches once.
_shared: dict = {"loop": None}


def _shared_state() -> dict:
    loop = asyncio.get_running_loop()
    if _shared.get("loop") is not loop:
        # First use, or a new event loop (e.g. tests): objects of another loop are unusable
        _shared.clear()
        _shared.update(loop=loop, lock=asyncio.Lock(), playwright=None,
                       connection=None, generation=None)
    return _shared


async def _shared_playwright() -> Playwright:
    state = _shared_state()
    async with state["lock"]:
        if state["playwright"] is None:
            state["playwright"] = await async_playwright().start()
        return state["playwright"]


async def _shared_context(browser: Browser, generation: int) -> BrowserContext:
    """The default context of the shared browser, through the shared connection. A
    new connection is made only when the browser was replaced or the old one broke."""
    state = _shared_state()
    async with state["lock"]:
        connection = state["connection"]
        if connection is not None and (state["generation"] != generation
                                       or not connection.is_connected()):
            state["connection"] = None
            with suppress(Exception):
                await asyncio.wait_for(connection.close(), timeout=5)
            connection = None
        if connection is None:
            if state["playwright"] is None:
                state["playwright"] = await async_playwright().start()
            try:
                connection = await state["playwright"].chromium.connect_over_cdp(
                    browser.get_endpoint_url(), timeout=30_000)
            except Exception:
                # The driver may be what failed; no page depends on it any more (no
                # live connection), so the next attempt starts a fresh one
                playwright, state["playwright"] = state["playwright"], None
                with suppress(Exception):
                    await asyncio.wait_for(playwright.stop(), timeout=5)
                raise
            state["connection"], state["generation"] = connection, generation
        return connection.contexts[0]


async def _tab_answers(session: aiohttp.ClientSession, ws_url: Optional[str]) -> bool:
    """Whether a tab still executes a trivial command, asked over its own DevTools
    connection (Chromium serves several clients per tab, so this disturbs no one)."""
    if not ws_url:
        return False
    try:
        async with session.ws_connect(ws_url, timeout=5, max_msg_size=0) as ws:
            await ws.send_json({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": "1"}})
            async with asyncio.timeout(TAB_ANSWER_TIMEOUT):
                async for message in ws:
                    data = message.json()
                    if data.get("id") == 1:
                        return "result" in data
    except Exception:
        return False
    return False


async def _close_orphaned_tabs(browser: Browser) -> int:
    """Closes the tabs that stopped answering and that no running retrieval owns:
    crashed ones, or those a retrieval left behind because its Playwright driver died.
    Every new Playwright connection hangs on such a tab, since it attaches to all of
    them. A tab that still answers is never closed, whatever the bookkeeping says: once,
    tabs whose registration had failed under load were taken for orphans and closed in
    the middle of their retrievals. Blank tabs are spared too: that is what a
    just-opened tab looks like, and what SeleniumBase's own first tab is."""
    if HeadedBrowser._cloudflare_lock.locked():
        return 0  # The Cloudflare solver's own tab is open and not registered
    try:
        port = urlparse(browser.get_endpoint_url()).port
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/json/list",
                                   timeout=aiohttp.ClientTimeout(total=5)) as response:
                targets = await response.json(content_type=None)
            candidates = [t for t in targets
                          if t.get("type") == "page" and t.get("id") not in _owned_tabs
                          and t.get("url", "") not in ("", "about:blank")
                          and not t.get("url", "").startswith("chrome://")]
            answers = await asyncio.gather(*(_tab_answers(session, t.get("webSocketDebuggerUrl"))
                                             for t in candidates))
            closed = 0
            for target, answering in zip(candidates, answers):
                if answering:
                    continue
                with suppress(Exception):
                    async with session.get(f"http://127.0.0.1:{port}/json/close/{target['id']}",
                                           timeout=aiohttp.ClientTimeout(total=5)):
                        closed += 1
        if closed:
            logger.info(f"Closed {closed} orphaned browser tab(s) that no retrieval owned.")
        return closed
    except Exception:
        logger.debug("Could not close orphaned browser tabs.", exc_info=True)
        return 0


# Seconds a freshly launched browser gets to report its first tab
FIRST_TARGET_WAIT = 15


def _wait_for_first_target_on_create() -> None:
    """Makes SeleniumBase wait for the new browser's first tab before using it.

    `cdp_driver.start()` launches Chromium and immediately takes `main_tab`, the first
    of the targets Chromium has reported so far. A persistent profile, with extensions
    and service workers to load, takes longer to report any, and under load that is
    long enough for the list to still be empty: `IndexError`, every time, until the
    browser fell back to a throwaway profile and lost every stored session. So
    `Browser.create()`, which `start()` calls just before, is made to wait for one."""
    original = Browser.create.__func__
    if getattr(original, "_waits_for_first_target", False):
        return

    async def create(cls, *args, **kwargs):
        browser = await original(cls, *args, **kwargs)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FIRST_TARGET_WAIT
        while not browser.targets and loop.time() < deadline:
            await asyncio.sleep(0.25)
            with suppress(Exception):
                await browser.update_targets()
        return browser

    create._waits_for_first_target = True
    Browser.create = classmethod(create)


_wait_for_first_target_on_create()


# Seconds a document URL gets to start its download, a bot check in front included
DOCUMENT_WAIT = 60
# Seconds after which a page that shows no bot check is taken for what it is
DOCUMENT_SETTLE = 5
# Seconds a file the browser was served (a spreadsheet) gets to turn into its download
DOWNLOAD_AFTER_RESPONSE_WAIT = 15


async def _fetch_in_browser(page: Page, url: str) -> tuple[Optional[bytes], Optional[str], str]:
    """`url` fetched through the browser's network stack (Network.loadNetworkResource),
    from a frame on its site, see `HeadedBrowser._document_bytes()`. Returns (bytes or
    None, content type, what happened)."""
    site = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    try:
        if not page.url.startswith(site + "/"):
            with suppress(PlaywrightError):
                await page.goto(site + "/", wait_until="domcontentloaded",
                                timeout=DOCUMENT_WAIT * 1000)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + DOCUMENT_WAIT
            while await _shows_bot_check(page) and loop.time() < deadline:
                await asyncio.sleep(1)  # Its check passes by the same JavaScript
        session = await page.context.new_cdp_session(page)
    except Exception as e:
        return None, None, f"{type(e).__name__}: {str(e).splitlines()[0][:100]}"
    stream = None
    try:
        frame_id = (await session.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
        resource = (await asyncio.wait_for(session.send("Network.loadNetworkResource", {
            "frameId": frame_id, "url": url,
            "options": {"disableCache": False, "includeCredentials": True}}),
            timeout=DOCUMENT_WAIT))["resource"]
        stream = resource.get("stream")
        if not resource.get("success") or not stream:
            reason = f"HTTP {resource.get('httpStatusCode')} {resource.get('netErrorName', '')}"
            return None, None, reason.strip()
        headers = {k.lower(): v for k, v in (resource.get("headers") or {}).items()}
        chunks = []
        while True:
            chunk = await asyncio.wait_for(
                session.send("IO.read", {"handle": stream, "size": 1 << 20}), timeout=60)
            piece = chunk.get("data", "")
            chunks.append(base64.b64decode(piece) if chunk.get("base64Encoded")
                          else piece.encode("latin-1"))
            if chunk.get("eof"):
                break
        return b"".join(chunks), headers.get("content-type"), "read"
    except Exception as e:
        return None, None, f"{type(e).__name__}: {str(e).splitlines()[0][:100]}"
    finally:
        if stream:
            with suppress(Exception):
                await session.send("IO.close", {"handle": stream})
        with suppress(Exception):
            await session.detach()


class _BotCheckAgain(RetrievalFailed):
    """A document, asked for again outside the page, was answered with its bot check."""


def _complete_file(data: Optional[bytes], headers: dict) -> bool:
    """Whether `data` is the whole file the headers announce (a PDF ends with %%EOF)."""
    if not data:
        return False
    length = headers.get("content-length", "")
    if length.isdigit():
        return len(data) == int(length)
    if "pdf" in headers.get("content-type", ""):
        return data.rstrip().endswith(b"%%EOF")
    return False


async def _shows_bot_check(page: Page) -> bool:
    """Whether the page shows a CAPTCHA or bot check (see `captcha_detect`)."""
    from scrapemm.server.captcha_detect import detect_captcha
    try:
        return bool(detect_captcha(ScrapedContent(html=await page.content())))
    except Exception:
        return True  # Mid-navigation, as a passing check navigates: not settled yet


# Statuses of a page's final document that mean the site's server (or its CDN) failed
# rather than answered. Not 503: bot checks answer with it (insse.ro, older Cloudflare).
# Server errors whose page is never content: the server's own (500, 503), a gateway's
# (502, 504) and Cloudflare's when the server behind it fails (520-526, 530, e.g. "error
# code: 522" when the origin times out). A 503 may also be Cloudflare's challenge, which
# is told apart (see `_failed_server()`).
GATEWAY_ERRORS = (500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530)


def _note_served_document(response, page: Page, served: list[str],
                          statuses: list[int]) -> None:
    """Notes the status of each document the page's own navigation was answered with,
    and the content type of a document file (a PDF, a spreadsheet) among them, see
    `HeadedBrowser._document()`."""
    try:
        if not (response.request.is_navigation_request() and response.frame == page.main_frame):
            return
        statuses.append(response.status)
        from scrapemm.server.download.documents import is_document_content_type
        if (not served and response.ok
                and is_document_content_type(response.headers.get("content-type"))):
            served.append(response.headers.get("content-type"))
            page._scrapemm_served_response = response  # Its body may hold the whole file
    except Exception:
        pass  # Observing only; never the reason a retrieval fails

# Concurrent retrievals in the shared browser; override with
# `update_config(max_browser_pages=...)`
DEFAULT_MAX_BROWSER_PAGES = 32

_page_gate: Optional[asyncio.Semaphore] = None
_page_gate_limit: Optional[int] = None


# Concurrent browser retrievals per site, whatever the overall limit
MAX_BROWSER_PAGES_PER_DOMAIN = 6
_domain_gates: dict[str, asyncio.Semaphore] = {}


def _domain_gate(domain: str) -> asyncio.Semaphore:
    return _domain_gates.setdefault(domain, asyncio.Semaphore(MAX_BROWSER_PAGES_PER_DOMAIN))


def _browser_page_gate() -> asyncio.Semaphore:
    global _page_gate, _page_gate_limit
    limit = max(1, int(get_config_var("max_browser_pages") or DEFAULT_MAX_BROWSER_PAGES))
    if _page_gate is None or _page_gate_limit != limit:
        _page_gate, _page_gate_limit = asyncio.Semaphore(limit), limit
    return _page_gate


class _BrowserSlot:
    """A retrieval's place in the browser: one of its site's slots and one of the
    overall ones. Released as soon as the retrieval's page is closed, which may be
    before the retrieval ends: downloads that need no page (embedded videos via yt-dlp)
    must not keep other pages from opening."""

    def __init__(self, domain: str):
        self._gates = [_domain_gate(domain), _browser_page_gate()]
        self._held: list[asyncio.Semaphore] = []

    async def acquire(self) -> None:
        # The site's slot first: waiting for it must not hold one of the overall slots
        for gate in self._gates:
            await gate.acquire()
            self._held.append(gate)

    def release(self) -> None:
        while self._held:
            self._held.pop().release()


def _chromium_pids() -> set[int]:
    """The processes of every Chromium running here (Linux only; elsewhere empty). The
    shared browser is the only one scrapeMM launches, recognised by its debugging port."""
    pids = set()
    if not os.path.isdir("/proc"):
        return pids
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                if b"--remote-debugging-port" in f.read():
                    pids.add(int(entry))
        except OSError:
            continue  # Gone in the meantime
    return pids


def _kill_pids(pids: set[int]) -> None:
    import signal
    for pid in pids:
        with suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    if pids:
        logger.info(f"Killed {len(pids)} process(es) of a browser that failed to start.")


async def _browser_alive(browser: Optional[Browser]) -> bool:
    """Whether the browser process runs and still answers on its debugging port. Tells a
    dead browser apart from a lost tab or a connection attempt that merely timed out
    under load -- which restarting the shared browser would turn into a failure of
    every retrieval that has a page open in it."""
    if browser is None or browser.stopped:
        return False
    try:
        port = urlparse(browser.get_endpoint_url()).port
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/json/version",
                                   timeout=aiohttp.ClientTimeout(total=5)) as response:
                return response.status == 200
    except Exception:
        return False


# Seconds to give a Cloudflare challenge to clear by itself, before and after solving it
CLOUDFLARE_SELF_CLEAR_WAIT = 6
CLOUDFLARE_SOLVE_ATTEMPTS = 3
# Bounds the wait for the solver and its run, which drives the browser through CDP calls
# without timeouts of their own: one that hung held the solver's lock, and every page
# behind a challenge then waited out the full retrieval timeout (thip.media, 10 minutes)
CLOUDFLARE_SOLVE_TIMEOUT = 120

# Reading a page that navigates meanwhile (see `HeadedBrowser._html_and_source()`)
REDIRECT_READ_ATTEMPTS = 3
ANUBIS_WAIT = 20  # Seconds for Anubis' proof of work, which takes a second or two
_ANUBIS_MARKER = "anubis_challenge"  # The id of the script holding its challenge

# The challenge page, not the bot-management scripts ordinary Cloudflare pages carry too
_CLOUDFLARE_CHALLENGE_EXPR = ("/^just a moment/i.test(document.title) || !!document.querySelector("
                              "'#challenge-form, #challenge-running, #challenge-error-text')")
_CLOUDFLARE_CHALLENGE_JS = f"() => {_CLOUDFLARE_CHALLENGE_EXPR}"  # For Playwright


async def _shows_cloudflare_challenge(page: Page) -> bool:
    try:
        return bool(await page.evaluate(_CLOUDFLARE_CHALLENGE_JS))
    except Exception:
        return False  # E.g. mid-navigation. Best-effort: never the reason a retrieval fails


async def _wait_for_cloudflare(page: Page, seconds: float) -> bool:
    """Waits up to `seconds` for the challenge to go away. Returns whether it did."""
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1)
        with suppress(PlaywrightError):
            if not await page.evaluate(_CLOUDFLARE_CHALLENGE_JS):
                return True
    return False


async def _solve_cloudflare_in_own_tab(url: str) -> bool:
    """Opens `url` in a new tab driven by SeleniumBase alone and clicks the challenge
    there (see `HeadedBrowser._pass_cloudflare()`). Returns whether it cleared."""
    browser = HeadedBrowser._browser
    if browser is None:
        return False
    tab = None
    try:
        tab = await browser.get(url, new_tab=True)
        for _ in range(CLOUDFLARE_SOLVE_ATTEMPTS):
            await asyncio.sleep(3)
            if not await tab.evaluate(_CLOUDFLARE_CHALLENGE_EXPR):
                return True
            with suppress(Exception):
                await tab.solve_captcha()
        await asyncio.sleep(4)
        return not await tab.evaluate(_CLOUDFLARE_CHALLENGE_EXPR)
    except Exception:
        logger.debug(f"Solving the Cloudflare challenge at {url} failed.", exc_info=True)
        return False
    finally:
        if tab is not None:
            with suppress(Exception):
                await asyncio.wait_for(tab.close(), 10)


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
     mechanisms (e.g., Cloudflare) when retrieving web content. See `Browser` for the
    generic retrieval method built on it."""
    name = "Headed Browser"
    domains = []
    # Whether a page answering 404/410 counts as missing rather than as content. Off for
    # the archive integrations: a replay may pass on the archived page's own status.
    fails_on_not_found = False
    waits_for_browser_slot = True  # Its time starts with a slot, see `_get()`
    # Seconds a page gets to load its document, and whether a page that missed that is
    # tried once more on a new tab (under load, a page that is quick alone can miss it)
    navigation_timeout: ClassVar[float] = 60
    retry_on_timeout: ClassVar[bool] = True

    # Shared UC browser for all HeadedBrowser integrations (Perma.cc, Archive.org, …).
    _browser: ClassVar[Optional[Browser]] = None
    # Increments every time the shared browser is (re)started. Used to coordinate crash
    # recovery across concurrent tasks so only one of them actually restarts it.
    _generation: ClassVar[int] = 0
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _cloudflare_lock: ClassVar[asyncio.Lock] = asyncio.Lock()

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

    async def probe(self) -> None:
        """Reports readiness *without* starting the shared browser.

        The dashboard polls this, and launching a browser on every poll would be bad
        enough on its own -- but it would also fight the CAPTCHA panel and the running
        retrievals over the profile lock, and could restart the very browser somebody
        is in the middle of solving a check in. So this only establishes that the
        preconditions hold: a Chromium binary and, on Linux, a display to put it on.
        """
        browser = HeadedBrowser._browser
        if browser is not None and not browser.stopped:
            self.connected = True
            return

        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            self.connected = False
            raise RuntimeError(
                "No DISPLAY: the headed browser cannot start. In Docker this means Xvfb "
                "is not running.")

        try:
            async with async_playwright() as p:
                executable = _resolve_browser_executable(p)
        except Exception as e:
            self.connected = False
            raise RuntimeError(f"Playwright is not usable: {e}") from e

        if executable and not os.path.exists(executable):
            self.connected = False
            raise RuntimeError(f"The browser binary is missing at {executable}. "
                               f"Run `playwright install chromium`.")

        # Not started yet, but nothing stands in the way of starting it on demand
        self.connected = True

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
                _start_storage_watch()
            return HeadedBrowser._browser, HeadedBrowser._generation

    async def _start_browser_locked(self, playwright: Optional[Playwright] = None):
        """Starts the shared UC browser. Caller must already hold `_lock`.

        Runs on a persistent profile so that sessions established in this browser (see
        `ArchiveToday.capture_session()`) survive a restart. If that profile cannot be
        used -- it is corrupted, or another scrapeMM process already holds it, since
        Chromium allows only one instance per profile -- the browser is started on a
        throwaway profile instead: retrieving without persistence beats not retrieving.
        """
        # On Linux, SeleniumBase starts a virtual display of its own -- unless told the
        # session is already headed. Letting it do so is what made the CAPTCHA panel show
        # a black frame: SeleniumBase put Chromium on a display it had just created,
        # while x11vnc was exposing the one the container started (DISPLAY). Whenever a
        # display already exists, the browser has to go on *that* one, or nobody can see
        # it. Without one, SeleniumBase's own display is still the right answer.
        has_display = bool(os.environ.get("DISPLAY"))
        headed = has_display
        xvfb_metrics = (None if has_display or not sys.platform.startswith("linux")
                        else "1920,1080")
        executable_path = _resolve_browser_executable(playwright)
        user_agent = get_config_var("browser_user_agent")

        # Starting can fail transiently: under load, SeleniumBase may look for the first
        # tab before Chromium opened it ("main_tab": IndexError). So each profile gets a
        # second try before the persistent one is given up for a throwaway one.
        persistent = _resolve_profile_dir()
        if persistent:
            _forget_open_tabs(Path(persistent))
            _forget_replay_storage(Path(persistent))
        attempts = [persistent, persistent, None] if persistent else [None, None]
        for i, profile in enumerate(attempts):
            existing = _chromium_pids()
            try:
                logger.debug(f"Starting headed browser: {executable_path or 'system default'} "
                             f"(profile: {profile or 'throwaway'})")
                HeadedBrowser._browser = await cdp_driver.start_async(
                    headless=False,
                    headed=headed,
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
                # A start that failed half-way may have launched Chromium anyway, without
                # handing it over. Left running, it would keep the profile locked and eat
                # memory, so whatever this attempt launched goes.
                _kill_pids(_chromium_pids() - existing)
                retrying_same = i + 1 < len(attempts) and attempts[i + 1] == profile
                if retrying_same:
                    logger.info(f"Starting the browser failed; trying once more.", exc_info=True)
                    continue
                if profile is not None:
                    logger.warning(
                        f"Could not start the browser on its persistent profile ({profile}). "
                        f"Falling back to a throwaway profile, so sessions will not persist. "
                        f"This is expected if another scrapeMM process is already running.",
                        exc_info=True,
                    )
                    continue
                logger.error(f"Failed to start/restart the shared browser for integration: "
                             f"{self.name}", exc_info=True)

    async def _prepare_context(self, context: BrowserContext) -> None:
        """Optional hook before a new page is created (e.g. inject cookies)."""
        return

    async def _settle_after_goto(self, page: Page) -> None:
        """Optional post-navigation settle. Override in subclasses for content-specific readiness."""
        return

    def _watch_page(self, page: Page) -> None:
        """Optional hook on a fresh page before navigation, e.g. to observe its network
        traffic. Per page rather than per context: the context is shared by every
        retrieval running at the same time."""
        return

    async def _new_page(self, p: Optional[Playwright] = None, attempts: int = 3) -> tuple[Page, int]:
        """Open a new tab in the UC browser, through the connection all retrievals share
        (`p` is not needed for that; it is accepted for existing callers). Returns the
        page along with the browser generation it was opened on, so callers can report a
        crash precisely. Close the page with `release_page()`."""
        bad_generation = None
        for attempt in range(attempts):
            browser, generation = await self._ensure_browser(
                bad_generation, playwright=p or await _shared_playwright())
            if browser is None:
                if attempt < attempts - 1:
                    logger.debug(f"Shared browser unavailable, attempt {attempt + 1} failed, retrying...")
                    continue
                raise RuntimeError(f"The shared browser is not connected.")

            try:
                context = await _shared_context(browser, generation)
                await self._prepare_context(context)
                # Shielded: a tab whose opening is cancelled half-way still opens, and
                # would then stay open for good. So it is closed once it is there.
                # Behind the tab a human is solving a CAPTCHA in, if there is one
                human = _opening_human_tab.get()
                creating = asyncio.ensure_future(
                    _open_background_tab(context) if not human and _shown_human_tab()
                    else context.new_page())
                try:
                    page = await asyncio.wait_for(asyncio.shield(creating), timeout=30)
                except BaseException:
                    creating.add_done_callback(_close_late_tab)
                    raise
                try:
                    # Registered while still blank, so the orphan sweep never takes it
                    await _own(page)
                except BaseException:
                    await release_page(page)
                    raise
                if human:
                    _become_human_tab(page)
                return page, generation

            except (PlaywrightError, asyncio.TimeoutError) as e:
                if attempt < attempts - 1:
                    if await _browser_alive(browser):
                        # Busy, not dead: restarting it would kill every page open in
                        # it. A connection attempt also hangs on tabs that no longer
                        # answer (crashed, or left behind by a retrieval whose driver
                        # died), because it attaches to every tab -- so those go.
                        closed = await _close_orphaned_tabs(browser)
                        logger.debug(f"Connection attempt {attempt + 1} failed, but the "
                                     f"browser is alive; closed {closed} orphaned tab(s), "
                                     f"retrying.")
                        bad_generation = None
                        await asyncio.sleep(1)
                    else:
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
        """Retrieves the URL in the shared browser, as one of at most a few pages at a
        time. Archive pages are heavy -- a replayed video alone can take hundreds of
        megabytes -- so a whole test suite's worth of them at once exhausted
        memory, crashed tabs and pushed the rest past their timeouts. Waiting for a slot
        costs less than that."""
        # Per site too: many pages from one address at once is what trips bot checks.
        # Perma.cc answered 20 at once with Cloudflare challenges, and its replays then
        # never loaded; up to 8 at once went through cleanly.
        slot = _BrowserSlot(get_domain(url) or "")
        try:
            waiting_since = time.time()
            await slot.acquire()
            timing.wait_for_slot(time.time() - waiting_since)
            timing.work_started()
            budget.start(BROWSER_RETRIEVAL_TIMEOUT)  # Inherited by the task below
            # Not asyncio.wait_for(): that waits for the cancelled retrieval to wind
            # down, and a stuck one may never do so. The slot is freed right away instead.
            task = asyncio.ensure_future(self._browse(url, slot=slot, **kwargs))
            try:
                done, _ = await asyncio.wait({task}, timeout=BROWSER_RETRIEVAL_TIMEOUT)
            except asyncio.CancelledError:
                task.cancel()  # The caller gave up (e.g. a run was stopped): so does this
                raise
            if task in done:
                return task.result()
            task.cancel()
            task.add_done_callback(lambda t: t.cancelled() or t.exception())  # No "never retrieved" noise
            raise RetrievalFailed(f"{self.name} did not finish retrieving {url} within "
                                  f"{BROWSER_RETRIEVAL_TIMEOUT // 60} minutes; given up.")
        finally:
            slot.release()

    async def _browse(self, url: str, slot: Optional[_BrowserSlot] = None,
                      **kwargs) -> ScrapedContent:
        """Opens `url` in a tab of its own and extracts it. The tab is closed, and `slot`
        released, as soon as the page is no longer needed -- before media downloads that
        do not need it (see `resolve_media()`)."""
        target_url = url  # What is loaded; after a renderer crash, maybe a lighter page
        for attempt in range(2):  # one try + one crash-triggered retry
            page, generation = await self._new_page(None)
            media: Optional[BrowserMedia] = None
            page_open = True

            async def close_page():
                nonlocal page_open
                if page_open:
                    page_open = False
                    if media is not None:
                        await media.close()
                    await release_page(page)

            async def done_with_page():
                await close_page()
                if slot is not None:
                    slot.release()

            # A crashed renderer does not fail every call at once (a reload on it waits
            # out its timeout), so extraction steps can ask `renderer_crashed()`
            with suppress(AttributeError):  # Test doubles may lack events
                page.on("crash", lambda p: setattr(p, "_scrapemm_crashed", True))
            # A URL that serves a file (a spreadsheet, say) downloads instead of showing
            # a page -- often only after a bot check passed (see `_document()`)
            downloads: list = []
            downloading = False
            with suppress(AttributeError):  # A lambda: Playwright cannot wrap a bound builtin
                page.on("download", lambda download: downloads.append(download))
            # ...or shows the file in a viewer (PDFs). The page then has no DOM worth the
            # name, and on Linux its "domcontentloaded" may never come
            served: list[str] = []  # The document's content type, once it was served
            statuses: list[int] = []  # Of the page's own document, as it navigated
            with suppress(AttributeError):
                page.on("response", lambda r: _note_served_document(r, page, served, statuses))
            try:
                # Before navigating, so it sees every medium the page loads
                media = BrowserMedia(page)
                self._watch_page(page)
                await page.set_viewport_size({"width": 1920, "height": 1080})

                # domcontentloaded: return as soon as the DOM is parseable. Waiting for "load"
                # often burns many seconds on archive/analytics assets after content is ready.
                try:
                    response = await page.goto(target_url,
                                               timeout=self.navigation_timeout * 1000,
                                               wait_until="domcontentloaded")
                    # The final document's status: goto follows redirects (www -> bare)
                    if (self.fails_on_not_found and response is not None
                            and response.status in (404, 410)):
                        # The site's "not found" page is no content; the engine then
                        # turns to the archives
                        raise TargetUnavailableError(
                            f"{url} does not exist on the live site (HTTP {response.status}).")
                    if (self.fails_on_not_found and response is not None
                            and response.status == 451):
                        # Withheld from this server's region (e.g. US news sites for
                        # the EU); the engine then tries methods that fetch from elsewhere
                        raise RegionBlockedError(
                            f"{url} is blocked in this server's region (HTTP 451).")
                    if response is not None and "pdf" in response.headers.get("content-type", ""):
                        # The browser shows a PDF in its viewer, from which there is no
                        # text to extract: the file is read instead (see `_document()`)
                        if self.fails_on_not_found:
                            served[:] = served or [response.headers.get("content-type")]
                            return await self._document(page, downloads, url, served)
                        raise RetrievalFailed(f"{url} is a PDF, which {self.name} cannot "
                                              f"extract the text of.")
                except PlaywrightError as e:
                    if is_network_failure(e):
                        # The host cannot be reached from here: no second page will
                        # change that, but other methods (remote services) still may
                        raise RetrievalFailed(f"{self.name} could not reach {url}: "
                                              f"{str(e).splitlines()[0]}") from e
                    if (self.fails_on_not_found and (served or downloads)
                            and isinstance(e, PlaywrightTimeoutError)):
                        # A document, which the viewer shows without ever finishing a DOM
                        return await self._document(page, downloads, url, served)
                    if (attempt == 0 and self.retry_on_timeout
                            and isinstance(e, PlaywrightTimeoutError)):
                        # With dozens of heavy pages loading at once, a page that
                        # takes seconds on its own can miss the deadline; it is a
                        # matter of load, and worth another try on a fresh page
                        logger.info(f"Loading {url} timed out; retrying once on a new page.")
                        continue
                    downloading = "download is starting" in str(e).lower()
                    if not downloading and not self._is_client_redirect_abort(e):
                        raise
                    # The page already redirected itself; give the new document a moment
                    # to settle instead of failing the whole retrieval over a benign race.
                    logger.debug(f"Navigation to {url} was superseded by a client-side "
                                 f"redirect (net::ERR_ABORTED); continuing on {page.url}.")
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                if self.fails_on_not_found and (downloads or downloading or served
                                                or document_extension(url)):
                    if (document := await self._document(page, downloads, url, served)) is not None:
                        return document
                await self._pass_cloudflare(page)
                await self._settle_after_goto(page)
                if (statuses and statuses[-1] in GATEWAY_ERRORS and not served
                        and not await _shows_cloudflare_challenge(page)):
                    # The server (or its CDN) failed: its error page is no content. For
                    # an archive, it is the archive that is down, not the capture gone.
                    if self.fails_on_not_found:
                        raise RetrievalFailed(f"{url} answered HTTP {statuses[-1]}.")
                    raise RetrievalFailed(f"{self.name} is unavailable right now: it answered "
                                          f"HTTP {statuses[-1]} for {url}.")

                if target := await self._extract_content(page):
                    shown_url = getattr(page, "url", "") or ""  # What the content came from
                    # Before reading and before any screenshot: never content, and on
                    # some pages nearly all of the HTML (see `remove_consent_dialogs()`)
                    if get_config_var("remove_consent_dialogs", True) is not False:
                        await remove_consent_dialogs(page)
                    await annotate_rendered_media(target)
                    html, source = await self._html_and_source(target, page)
                    if html:
                        # On request, the page as it is shown, now that it is loaded and
                        # before resolving the media may close it: no second load
                        png = await screenshot.capture(page) if screenshot.wanted() else None
                        # Media that need the page are resolved while it is open;
                        # `done_with_page` is called once only the others are left
                        from scrapemm.server.util import to_scraped_content
                        content = await to_scraped_content(
                            html, session=page.context.request,
                            output_format=kwargs.get("output_format", "multimodal"),
                            url=url, source_element=source, media=media,
                            max_video_size=kwargs.get("max_video_size"),
                            on_browser_done=done_with_page,
                        )
                        screenshot.keep(content, png)
                        if media.stats:
                            logger.debug(f"Media of {url} came from: {dict(media.stats)}")
                        if "type=image" in shown_url and "type=image" not in url:
                            # A Perma.cc record's screenshot in place of its replay: not
                            # to be kept for good (see `cache.complete_capture()`)
                            content.stand_in = True
                        return content
                break  # No content found — not a crash, don't retry.

            except PlaywrightError as e:
                if attempt == 0 and page_open and "target crashed" in str(e).lower():
                    # The tab's renderer died; the browser is fine (it would say
                    # "closed"/"disconnected"). Some pages crash it every time, so the
                    # retry may load a lighter page instead (see `_after_renderer_crash()`)
                    target_url = self._after_renderer_crash(url)
                    logger.info(f"The page for {url} crashed its renderer; retrying on a new "
                                f"page{f' with {target_url}' if target_url != url else ''}.")
                    continue
                # Not once the page is closed: the slot is gone, and the error is not the page's
                if attempt == 0 and page_open and self._is_browser_crash(e):
                    if await _browser_alive(HeadedBrowser._browser):
                        # Only this tab was lost (e.g. a heavy page crashed its
                        # renderer), or another task already replaced the browser.
                        # Restarting now would kill every other retrieval's page.
                        logger.info(f"The page for {url} was lost ({type(e).__name__}); "
                                    f"the browser is fine, so retrying on a new page.")
                    else:
                        logger.warning(
                            f"The shared browser crashed while retrieving {url} with {self.name}; "
                            f"recovering and retrying once."
                        )
                        # Trigger (or await an already in-flight) single-flight recovery before retrying.
                        await self._ensure_browser(generation)
                    continue
                if attempt == 0 and page_open and "detached" in str(e).lower():
                    # A frame the retrieval was working in went away: the page replaced
                    # it (e.g. a replay reloading itself). A fresh page usually settles.
                    logger.info(f"A frame of {url} was detached mid-retrieval; retrying on "
                                f"a new page.")
                    continue
                if isinstance(e, PlaywrightTimeoutError):
                    raise  # The engine reports timeouts as such
                # Not a crash: as a failure of this method, not as a raw Playwright error
                raise RetrievalFailed(f"{self.name} failed in the browser at {url}: "
                                      f"{str(e).splitlines()[0][:300]}") from e
            finally:
                await close_page()

        raise RetrievalFailed(f"{self.name} integration was unable to extract content from {url}.")

    async def _document(self, page: Page, downloads: list, url: str,
                        served: list[str]) -> Optional[ScrapedContent]:
        """The document `url` serves as a file -- a download, or what the browser shows in
        its viewer (`served` has its content type) -- read into content by
        `documents.to_content()`, or None if it turns out to show a page after all. Waits
        for the file: a bot check in front of it ("Verifying your browser...") passes by
        itself in a few seconds, more under load. A file of a type scrapeMM does not read
        is reported as such -- the check in front of it was no CAPTCHA anybody has to
        solve."""
        from scrapemm.server.download import documents
        from scrapemm.common import UnsupportedDomainError
        loop = asyncio.get_running_loop()
        name = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or urlparse(url).netloc
        for attempt in range(2):
            start = loop.time()
            while not downloads and not served and loop.time() < start + DOCUMENT_WAIT:
                await asyncio.sleep(0.5)
                if loop.time() - start >= DOCUMENT_SETTLE and not await _shows_bot_check(page):
                    break  # A page and no file: asked for directly, below
            # A file the browser does not show (a spreadsheet) arrives as a download just
            # after its response: under load, seconds after. Its download beats asking again.
            if served and not downloads and "pdf" not in served[0]:
                settle = loop.time() + DOWNLOAD_AFTER_RESPONSE_WAIT
                while not downloads and loop.time() < settle:
                    await asyncio.sleep(0.5)
            if not downloads and not served:
                break
            try:
                download = downloads[0] if downloads else None
                if download is not None:
                    name = download.suggested_filename or name
                data, content_type = await self._document_bytes(page, url, download=download)
                break
            except _BotCheckAgain as e:
                if attempt:
                    raise RetrievalFailed(f"The document at {url} could not be downloaded: "
                                          f"the bot check in front of it answered again "
                                          f"({e}).") from e
                # Asked for outside the page, the file met the bot check once more. In the
                # browser the check passes (by its JavaScript): loaded once more, the file
                # arrives as a download or in the viewer
                logger.info(f"Asked for again, {url} answered with its bot check ({e}); "
                            f"loading it in the browser once more.")
                downloads.clear()
                served.clear()
                with suppress(AttributeError):
                    del page._scrapemm_served_response
                with suppress(PlaywrightError):
                    await page.goto(url, wait_until="commit", timeout=DOCUMENT_WAIT * 1000)
        if downloads or served:
            pass  # Read above
        elif await _shows_bot_check(page):
            raise RetrievalFailed(f"{url} should deliver a document, but the bot check in "
                                  f"front of it did not let the browser through.")
        else:
            # A page instead of the file. A passed bot check may have moved on to another
            # page (eur-lex.europa.eu's lands on its home page); asked for again with the
            # cookies it set, the URL may serve the file after all
            try:
                data, content_type = await self._document_bytes(page, url)
            except RetrievalFailed:
                return None
            if not documents.is_document_content_type(content_type):
                return None
            logger.info(f"The browser was shown a page for {url}; asked for again, it "
                        f"served the document.")
        content_type = (served[0] if served else None) or content_type
        if not documents.document_extension(name):
            name = url  # E.g. eur-lex.europa.eu's ".../TXT/PDF/?uri=...": the URL says more
        content = await asyncio.to_thread(documents.to_content, data, name, content_type)
        if content is None:
            kind = documents.document_extension(name) or content_type or "binary"
            raise UnsupportedDomainError(
                f"{url} is a {kind} file ({name}), a document type scrapeMM does not "
                f"extract text from.")
        logger.info(f"📄 {url} is the document {name}; read it into text.")
        return content

    @staticmethod
    async def _document_bytes(page: Page, url: str, download=None) -> tuple[bytes, Optional[str]]:
        """The bytes of the document `url`, taken from the browser, from the first source
        that holds the actual file (see `documents.is_file_of_type()`):

        * the download, copied out at once -- Playwright removes its temporary file when
          the connection that saw the download is replaced, which under load happened
          before the file was read;
        * what the viewer was served, whole unless the viewer cut it short to load ranges;
        * the file fetched by the browser's own network stack, from a frame on the file's
          site: the page itself if it shows the site, else the site's home page, loaded
          for it (from about:blank, where a download leaves the page, the site's cookies
          are not sent). The browser's TLS fingerprint and its clearance get through where
          Playwright's request context meets the check again (insse.ro: HTTP 503 every
          time, while the browser got the file);
        * the file asked for again with the browser's cookies and user agent.

        Under load, any of them turned out to hold the bot check's page instead of the
        file ("File is not a zip file"). If none holds the file but one met the bot check,
        `_BotCheckAgain` lets the caller load the URL in the browser once more. Returns
        (bytes, content type, if known)."""
        from scrapemm.server.download import documents
        kind = documents.document_extension(url)
        failures = []

        def usable(data, content_type, source) -> bool:
            if data and documents.is_file_of_type(data, kind, content_type):
                return True
            failures.append(f"{source}: {documents.describe(data, content_type)}")
            return False

        if download is not None:
            target = Path(tempfile.mkdtemp(prefix="scrapemm-document-")) / "file"
            try:
                await asyncio.wait_for(download.save_as(target), DOCUMENT_WAIT)
                data = await asyncio.to_thread(target.read_bytes)
                if usable(data, None, "the download"):
                    return data, None
            except Exception as e:
                failures.append(f"the download: {type(e).__name__}")
            finally:
                with suppress(Exception):
                    await download.delete()
                with suppress(OSError):
                    target.unlink()
                    target.parent.rmdir()
        if response := getattr(page, "_scrapemm_served_response", None):
            content_type = response.headers.get("content-type")
            try:
                data = await asyncio.wait_for(response.body(), DOCUMENT_WAIT)
                if _complete_file(data, response.headers) and usable(data, content_type,
                                                                      "the viewer"):
                    return data, content_type
            except Exception as e:
                failures.append(f"the viewer: {type(e).__name__}: {str(e).splitlines()[0][:100]}")
        data, content_type, outcome = await _fetch_in_browser(page, url)
        if data is not None and usable(data, content_type, "the browser's network"):
            return data, content_type
        if data is None:
            failures.append(f"the browser's network: {outcome}")
        try:
            # Certificate errors ignored, as the browser itself ignores them (see
            # `_browser_args()`): insse.ro serves an incomplete chain. With the browser's
            # own user agent: a bot check's clearance may hold only for the agent that
            # earned it.
            headers = {}
            with suppress(Exception):
                headers["User-Agent"] = await page.evaluate("navigator.userAgent")
            again = await page.context.request.get(url, timeout=DOCUMENT_WAIT * 1000,
                                                   ignore_https_errors=True, headers=headers)
            content_type = again.headers.get("content-type", "")
            data = await again.body() if again.ok else b""
            if again.ok and usable(data, content_type, "asked for again"):
                return data, content_type
            if not again.ok:
                failures.append(f"asked for again: HTTP {again.status}, {content_type}")
        except Exception as e:
            failures.append(f"asked for again: {type(e).__name__}: {e}")
        status = "; ".join(failures)
        logger.info(f"No source held the document {url}: {status}.")
        if any("html" in f or "HTTP 403" in f or "HTTP 429" in f or "HTTP 503" in f
               for f in failures):
            raise _BotCheckAgain(status)
        raise RetrievalFailed(f"The document at {url} could not be downloaded ({status}).")

    def _after_renderer_crash(self, url: str) -> str:
        """What to load on the new page after `url` crashed its tab's renderer. The same
        URL by default: a renderer may die of the load of dozens of heavy pages at once."""
        return url

    async def _pass_cloudflare(self, page: Page) -> None:
        """Gets the page past a Cloudflare challenge ("Just a moment..."), if it shows one.

        A real browser passes the non-interactive challenge on its own, but this one is
        driven through Playwright, whose instrumentation Cloudflare notices: it then asks
        for a click on its checkbox, and stays there. So the challenge is solved in a tab
        of the same browser that SeleniumBase drives alone, whose click Cloudflare
        accepts. The clearance cookie lands in the shared profile, and the Playwright page
        is reloaded with it. Best-effort: if it fails, the challenge page is what gets
        extracted, and the CAPTCHA detection takes it from there."""
        if not await _shows_cloudflare_challenge(page):
            return
        # The non-interactive challenge may still clear by itself
        if await _wait_for_cloudflare(page, CLOUDFLARE_SELF_CLEAR_WAIT):
            # It clears by navigating to the real page, and the challenge markers are gone
            # as soon as that page's <title> is parsed. Extracting right then got the title
            # and nothing else (lrkm.lrv.lt), so the new document gets to load first.
            with suppress(PlaywrightError):
                await page.wait_for_load_state("domcontentloaded", timeout=30_000)
            return
        logger.info(f"☁️ Cloudflare challenge at {page.url}; solving it in a SeleniumBase tab.")
        # One at a time: the click goes through the one shared browser window
        try:
            async with asyncio.timeout(CLOUDFLARE_SOLVE_TIMEOUT):
                async with HeadedBrowser._cloudflare_lock:
                    solved = await _solve_cloudflare_in_own_tab(page.url)
        except TimeoutError:
            logger.warning(f"Solving the Cloudflare challenge at {page.url} did not finish "
                           f"within {CLOUDFLARE_SOLVE_TIMEOUT} s; given up.")
            solved = False
        if not solved:
            logger.info(f"Could not get past the Cloudflare challenge at {page.url}.")
            return
        with suppress(PlaywrightError):
            await page.reload(wait_until="domcontentloaded", timeout=60_000)
        if await _wait_for_cloudflare(page, CLOUDFLARE_SELF_CLEAR_WAIT):
            with suppress(PlaywrightError):
                await page.wait_for_load_state("domcontentloaded", timeout=30_000)
            logger.info(f"☁️ Passed the Cloudflare challenge at {page.url}.")

    async def _html_and_source(
            self, target: ContentTarget, page: Page
    ) -> tuple[Optional[str], Page | Frame]:
        """Resolve HTML and a Frame/Page suitable for in-page media fetch.

        A page may still be replacing itself: an interstitial that redirects once its
        script is done, such as Anubis' proof-of-work check (newsmobile.in), navigates
        right while its HTML is read, and Playwright then refuses ("the page is
        navigating"). The new document is waited for and read instead."""
        if isinstance(target, ElementHandle):
            html = await target.evaluate("el => el.outerHTML")
            source = await target.owner_frame() or page
            return html, source
        for attempt in range(REDIRECT_READ_ATTEMPTS):
            last = attempt == REDIRECT_READ_ATTEMPTS - 1
            try:
                html = await target.content()
            except PlaywrightError as e:
                if last or "is navigating" not in str(e):
                    raise
                logger.debug(f"{page.url} was navigating while being read; waiting for it.")
                await self._await_new_document(page)
                continue
            if last or _ANUBIS_MARKER not in html:
                return html, target
            logger.debug(f"Waiting for {page.url} to pass its Anubis proof-of-work check.")
            with suppress(PlaywrightError):
                await page.wait_for_function(
                    f"() => !document.getElementById({_ANUBIS_MARKER!r})",
                    timeout=ANUBIS_WAIT * 1000)
            await self._await_new_document(page)
        return None, target  # Not reached

    async def _await_new_document(self, page: Page) -> None:
        """Lets the document a page navigated to load and build itself."""
        with suppress(PlaywrightError):
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        with suppress(PlaywrightError):
            await self._settle_after_goto(page)

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


# How the Browser method decides a page has finished building itself: its DOM, sampled
# every DOM_SETTLE_INTERVAL seconds, stayed within DOM_SETTLE_TOLERANCE of its size for
# DOM_STABLE_WINDOW seconds, and the document finished loading (readyState 'complete')
# or DOM_LOAD_WAIT seconds passed. Bounded by DOM_SETTLE_TIMEOUT, for pages that never
# stop changing (tickers, rotating ads), which are then taken as they are. A page with
# less than DOM_THIN_TEXT characters of text is most likely a shell still waiting for
# its content, and gets up to DOM_THIN_TIMEOUT.
DOM_SETTLE_INTERVAL = 0.25
DOM_SETTLE_TOLERANCE = 0.01
DOM_STABLE_WINDOW = 1.0
DOM_LOAD_WAIT = 4
DOM_SETTLE_TIMEOUT = 8
DOM_THIN_TEXT = 1000
DOM_THIN_TIMEOUT = 15

_DOM_STATE_JS = ("() => [document.documentElement ? document.documentElement.outerHTML.length : 0,"
                 " document.readyState]")


# How long a frame gets to answer one script, seconds. A frame busy with a heavy medium (a
# long video loading in an archive's replay) may not answer at all, and a call that never
# returns held the whole retrieval until its limit (perma.cc/75EG-E5GK: two minutes in
# Perma.cc's search for the frame with the media).
EVALUATE_TIMEOUT = 10


async def evaluate_within(target: Page | Frame, script: str, arg=None,
                          timeout: float = EVALUATE_TIMEOUT):
    """`target.evaluate()`, but a frame that does not answer in time raises Playwright's
    timeout error, which callers treat as any other failed evaluation (navigation, a
    detached frame), instead of being waited for."""
    call = target.evaluate(script) if arg is None else target.evaluate(script, arg)
    try:
        return await asyncio.wait_for(call, timeout)
    except asyncio.TimeoutError:
        raise PlaywrightTimeoutError(f"The frame did not answer within {timeout:.0f} s.") from None


async def _is_thin(target: Page | Frame, min_text: int) -> bool:
    try:
        return await evaluate_within(
            target, "() => (document.body ? document.body.innerText.length : 0)") < min_text
    except PlaywrightError:
        return True  # Mid-navigation: the next document is still to come


async def settle_dom(target: Page | Frame, min_text: int = 0) -> None:
    """Waits until a page or frame stops building itself (see DOM_SETTLE_*). With
    `min_text`, a document with less text than that counts as a shell still waiting for
    its content and gets up to DOM_THIN_TIMEOUT.

    Measured by time, not by a number of samples: while calls were slow (under load),
    three samples spanned seconds; once they got fast, they spanned half a second, and
    pages were taken in a pause of their build-up -- Animal Político's article before its
    text arrived, EFE Verifica's before its last blocks."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    anchor, since = None, start  # The size the DOM holds still at, and since when
    while (now := loop.time()) < start + (DOM_THIN_TIMEOUT if min_text else DOM_SETTLE_TIMEOUT):
        try:
            size, state = await evaluate_within(target, _DOM_STATE_JS)
        except PlaywrightError:
            size, state = None, None  # Mid-navigation: whatever comes next is a new page
        settled = False
        if size and anchor and abs(size - anchor) <= anchor * DOM_SETTLE_TOLERANCE:
            loaded = state == "complete" or now - start >= DOM_LOAD_WAIT
            settled = loaded and now - since >= DOM_STABLE_WINDOW
        else:
            anchor, since = size, now
        if settled or now - start >= DOM_SETTLE_TIMEOUT:
            # Only now, as it costs a layout: a shell (Animal Político's article sat at
            # 600 characters for 6 s before its text arrived) is worth waiting for
            if not min_text or not await _is_thin(target, min_text):
                return
            since = now  # Look again after another stable window
        await asyncio.sleep(DOM_SETTLE_INTERVAL)


CONSENT_FRAME_TIMEOUT = 1.0 # Seconds a frame has to answer the removal
# Removes the dialogs of known consent platforms (see `util.CONSENT_PLATFORMS`) from a
# document, and the scroll lock they put on it. Returns [bytes removed, [[id, classes]]].
_REMOVE_CONSENT_JS = """({ids, prefixes, classes}) => {
    const roots = new Set();
    for (const id of ids) { const e = document.getElementById(id); if (e) roots.add(e); }
    for (const p of prefixes) document.querySelectorAll(`[id^="${p}"]`).forEach(e => roots.add(e));
    for (const c of classes) for (const e of document.getElementsByClassName(c)) roots.add(e);
    let bytes = 0;
    const removed = [];
    for (const e of roots) {
        if (!e.isConnected || e === document.documentElement || e === document.body) continue;
        if (e.querySelector("main, article, [role=main]")) continue;  // Never the content
        bytes += e.outerHTML.length;
        removed.push([e.id || "", Array.from(e.classList)]);
        e.remove();
    }
    if (removed.length) {
        // The scroll lock the platforms put on the document while their dialog shows
        for (const el of [document.documentElement, document.body]) {
            if (!el) continue;
            el.classList.remove("CybotCookiebotDialogActive", "ot-overflow-hidden",
                                "sp-message-open", "didomi-popup-open", "qc-cmp-ui-showing");
            if (el.style.overflow === "hidden") el.style.overflow = "";
            if (el.style.overflowY === "hidden") el.style.overflowY = "";
        }
    }
    return [bytes, removed];
}"""


async def remove_consent_dialogs(page: Page) -> None:
    """Removes the dialogs of known consent platforms (Cookiebot, OneTrust, Sourcepoint,
    ...) from the page and its frames (archive replays show the captured page in one),
    and lifts the scroll lock they put on it. Such a dialog is never content (decided
    2026-10-08), but may be nearly all of the page: Cookiebot's lists over a thousand
    vendors, 4.9 of borkenerzeitung.de's 5 MB, which cost seconds to read, parse and
    convert. Gone, it also no longer covers the screenshot. Only the platforms' exact
    roots; looser matches are left to `strip`. Best-effort: a frame that fails is skipped."""
    from scrapemm.server.util import (CONSENT_PLATFORM_CLASSES, CONSENT_PLATFORM_IDS,
                                      CONSENT_PLATFORM_ID_PREFIXES, consent_platform_of)
    config = {"ids": sorted(CONSENT_PLATFORM_IDS), "prefixes": list(CONSENT_PLATFORM_ID_PREFIXES),
              "classes": sorted(CONSENT_PLATFORM_CLASSES)}
    async def clean(frame) -> None:
        try:
            # Timed: a frame that is still loading (an embedded YouTube player, say) may
            # never answer, and the whole retrieval would wait with it (aosfatos.org)
            removed_bytes, removed = await asyncio.wait_for(
                frame.evaluate(_REMOVE_CONSENT_JS, config), CONSENT_FRAME_TIMEOUT)
        except Exception:  # Detached, navigating or silent (or a test double): nothing to remove
            logger.debug(f"Could not look for consent dialogs in {getattr(frame, 'url', '?')}.",
                         exc_info=True)
            return
        if removed:
            platforms = sorted({consent_platform_of(i, c) or "?" for i, c in removed})
            logger.info(f"🍪 Removed the consent dialog of {', '.join(platforms)} "
                        f"({removed_bytes / 1e6:.2f} MB) from {frame.url[:120]}.")

    # A frame without an address has not navigated anywhere yet and never answers
    frames = [f for f in getattr(page, "frames", None) or [page] if getattr(f, "url", "?") != ""]
    await asyncio.gather(*(clean(frame) for frame in frames))


class Browser(HeadedBrowser):
    """The Browser retrieval method: the shared headed browser on any page of the open
    web. The default there, tried before Firecrawl and Decodo (see
    `scrapemm.server.engine`). Not tied to any domain, so it is called through `_get()`,
    not `get()`."""
    name = "Browser"
    domains = []
    fails_on_not_found = True

    async def _settle_after_goto(self, page: Page) -> None:
        """Waits until the page stops building itself. At `domcontentloaded` the markup is
        parsed, but scripts may still be adding the content: Kyiv Independent's page grew
        by another sixth over the next 1.5 s, and UNDP's was sometimes taken at less than
        half its size, with most of the text missing."""
        await settle_dom(page, min_text=DOM_THIN_TEXT)
