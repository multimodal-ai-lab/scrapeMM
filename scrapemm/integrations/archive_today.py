"""Integration for Archive.today retrieval.

Archive.today gates every snapshot's replay page behind a Google reCAPTCHA (served with
HTTP 429). Passing it yields a `cf_clearance` cookie that unlocks the page for **exactly
five minutes** — activity does not extend it, and nothing but solving the CAPTCHA again
renews it (measured 2026-09-21). scrapeMM does not solve CAPTCHAs.

Given that, retrieval works like this:

1. **Fetch the replay page over plain HTTP** with the stored `cf_clearance`. The page's
   media lives on ungated `*.archive.ph` subdomains, so once the HTML is in hand the
   images and videos download without any session. No browser is involved.
2. **If the page is gated** and interactive solving is enabled (default), open the
   snapshot in scrapeMM's own browser and wait for a human to pass the check, then store
   the fresh session and retry step 1. On a headless machine the browser window is
   reachable through an SSH tunnel, see `remote_view_hint()`.
3. **Otherwise**, if the screenshot fallback is enabled, serve what Archive.today offers
   without the check: the snapshot's full-page screenshot plus its original URL, capture
   time and title (resolved through the ungated `cse.js` and capture-listing endpoints).
4. **Otherwise**, raise, with a hint on how to establish a session.
"""

import asyncio
import html as html_lib
import json
import logging
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page

from scrapemm import CaptchaEncounteredError
from scrapemm.common.exceptions import RetrievalFailed, TargetUnavailableError
from scrapemm.common import get_config_var, update_config
from scrapemm.common.paths import CONFIG_DIR
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.download.requests import request_static
from scrapemm.integrations.headed_browser import HeadedBrowser, remote_view_hint
from scrapemm.secrets import get_secret, set_secret
from scrapemm.util import parse_cookies, to_scraped_content

logger = logging.getLogger("scrapeMM")

# All Archive.today mirrors are one and the same service: they resolve to the same
# servers, snapshot ids are global (every mirror answers identically for a given id), and
# archive.today itself redirects to archive.ph. The only thing that differs per mirror is
# cookie scope -- the session token earned by passing the access check is bound to the
# domain it was earned on. So every mirror URL is rewritten to this one domain before it
# is touched, and a single session covers everything.
CANONICAL_DOMAIN = "archive.ph"

# The div that wraps a snapshot's replayed content on the page
CONTENT_DIV_ID = "CONTENT"

# The session cookie a solved access check yields. It alone unlocks the replay page.
CLEARANCE_COOKIE = "cf_clearance"

# Any snapshot triggers the same access check, so an arbitrary id serves to establish a
# session when no particular one is being retrieved (manual `capture_session()`).
VERIFICATION_SNAPSHOT = "Ubqsd"

# Markers of Archive.today's access check. It is shown both as a bot challenge and as the
# body of a 429 response when the IP address sent too many requests recently.
GATE_MARKERS = ("security check", "captcha", "just a moment",
                "performing security verification", "recaptcha")

# Snapshot ids are five alphanumeric characters, e.g. https://archive.ph/uTVE4
SHORT_ID_REGEX = re.compile(r"^/([A-Za-z0-9]{5})(?:/.*)?$")

# Long form of a snapshot URL: /2022.05.05-091515/https://example.com/... (dotted) or
# /20220505091515/https://example.com/... (Memento style). The original URL is often
# mangled to a single slash after the scheme ("https:/example.com") by URL normalizers.
LONG_FORM_REGEX = re.compile(r"^/(\d{4})\.?(\d{2})\.?(\d{2})-?(\d{2})(\d{2})(\d{2})/(.+)$")

# Resolved snapshots never change, so they are kept forever
SNAPSHOT_CACHE_PATH = CONFIG_DIR / "archive_today_snapshots.json"

# Whether a gated snapshot is opened in scrapeMM's browser for a human to solve the access
# check on the spot. On by default; disable for unattended runs with
# `update_config(archive_today_interactive_solve=False)`.
INTERACTIVE_SOLVE_CONFIG_KEY = "archive_today_interactive_solve"
SOLVE_TIMEOUT_CONFIG_KEY = "archive_today_solve_timeout"
DEFAULT_SOLVE_TIMEOUT = 300

# Whether a snapshot whose page is behind the access check is served as its screenshot
# plus metadata instead of failing. Off by default: a screenshot is no substitute for the
# page text, so the failure (with its hint to establish a session) is the more honest
# result. Enable with `update_config(archive_today_screenshot_fallback=True)`.
SCREENSHOT_FALLBACK_CONFIG_KEY = "archive_today_screenshot_fallback"

# Archive.today's own Custom Search Engine helper. It hands out the original URL, the
# capture time and the content hash of any snapshot id -- without the access check.
CSE_URL_REGEX = re.compile(r'innerHTML = "((?:[^"\\]|\\.)*)"')
CSE_TIME_REGEX = re.compile(r">(\d{1,2} [A-Za-z]{3} \d{4} \d{2}:\d{2}:\d{2}) UTC<")
CSE_HASH_REGEX = re.compile(r"/[A-Za-z0-9]{5}/([0-9a-f]{40})/thumb\.png")

# One capture row on the listing page that Archive.today shows for an original URL
LISTING_ROW_REGEX = re.compile(
    r'href="https?://[^/"]+/([A-Za-z0-9]{5})"><img[^>]*?title="([^"]*)"[^>]*'
    r'src="/[A-Za-z0-9]{5}/([0-9a-f]{40})/thumb\.png"[^>]*><div[^>]*>([^<]+)</div>')

# Archive.today localizes the capture times it prints according to Accept-Language
# ("14 Feb. 2022" instead of "14 Feb 2022" for German, say). The parsers below expect
# the English form, so every lookup asks for it explicitly.
LOOKUP_HEADERS = {"Accept-Language": "en-US,en;q=0.9"}

GATE_HINT = (
    "Archive.today asks to solve a captcha. Cannot access the archived page's text. "
    "scrapeMM does not solve captchas. Run scrapemm.configure_archive_today_session() to "
    "pass the check yourself once in scrapeMM's own browser; the resulting session lasts "
    "about five minutes. Alternatively, "
    "update_config(archive_today_screenshot_fallback=True) makes scrapeMM serve the "
    "snapshot's screenshot and metadata instead of failing."
)


def _looks_like_gate(body_text: str) -> bool:
    """True if the page is Archive.today's access check rather than actual content."""
    return any(marker in body_text.lower() for marker in GATE_MARKERS)


def _interactive_solve_enabled() -> bool:
    return bool(get_config_var(INTERACTIVE_SOLVE_CONFIG_KEY, True))


def _solve_timeout() -> float:
    return float(get_config_var(SOLVE_TIMEOUT_CONFIG_KEY, DEFAULT_SOLVE_TIMEOUT))


def _screenshot_fallback_enabled() -> bool:
    return bool(get_config_var(SCREENSHOT_FALLBACK_CONFIG_KEY, False))


def canonicalize_url(url: str) -> str:
    """Rewrites any Archive.today mirror URL to its https equivalent on CANONICAL_DOMAIN."""
    parsed = urlparse(url)
    return urlunparse(parsed._replace(scheme="https", netloc=CANONICAL_DOMAIN))


@dataclass
class Snapshot:
    """What Archive.today knows about a capture, obtained without passing the access check."""
    id: str  # The five-character short id
    original_url: str
    captured_at: str  # ISO 8601, UTC
    hash: str  # Content hash that addresses the snapshot's screenshot and thumbnail
    title: Optional[str] = None

    @property
    def canonical_url(self) -> str:
        return f"https://{CANONICAL_DOMAIN}/{self.id}"

    @property
    def screenshot_url(self) -> str:
        """Full-page screenshot of the capture. Served without the access check."""
        return f"https://{CANONICAL_DOMAIN}/{self.id}/{self.hash}/scr.png"


class _SnapshotCache:
    """Permanent, file-backed map from snapshot URL path to the resolved Snapshot."""

    def __init__(self, path: Path = SNAPSHOT_CACHE_PATH):
        self.path = path
        self._entries: Optional[dict[str, dict]] = None

    def _load(self) -> dict[str, dict]:
        if self._entries is None:
            try:
                self._entries = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._entries = {}
        return self._entries

    def get(self, key: str) -> Optional[Snapshot]:
        if entry := self._load().get(key):
            return Snapshot(**entry)
        return None

    def put(self, key: str, snapshot: Snapshot) -> None:
        entries = self._load()
        entries[key] = asdict(snapshot)
        try:
            self.path.write_text(json.dumps(entries, indent=1), encoding="utf-8")
        except OSError:
            logger.debug(f"Could not persist the Archive.today snapshot cache at {self.path}.",
                         exc_info=True)


_snapshots = _SnapshotCache()


async def _fetch_text(url: str, session: Optional[aiohttp.ClientSession]) -> Optional[str]:
    if session is not None:
        return await request_static(url, session=session, headers=LOOKUP_HEADERS)
    async with aiohttp.ClientSession() as own_session:
        return await request_static(url, session=own_session, headers=LOOKUP_HEADERS)


def _parse_cse(short_id: str, script: str) -> Optional[Snapshot]:
    """Parses Archive.today's cse.js output. For an unknown id the script is a stub
    without any of the three pieces of information."""
    url_match = CSE_URL_REGEX.search(script)
    time_match = CSE_TIME_REGEX.search(script)
    hash_match = CSE_HASH_REGEX.search(script)
    if not (url_match and time_match and hash_match):
        return None
    original_url = json.loads(f'"{url_match.group(1)}"')  # JS string escapes, e.g. ı
    captured_at = datetime.strptime(time_match.group(1), "%d %b %Y %H:%M:%S").replace(tzinfo=timezone.utc)
    return Snapshot(id=short_id, original_url=original_url, captured_at=captured_at.isoformat(),
                    hash=hash_match.group(1))


async def resolve_snapshot(short_id: str,
                           session: Optional[aiohttp.ClientSession] = None) -> Optional[Snapshot]:
    """Looks up the original URL, capture time and content hash of a snapshot by its short
    id, using Archive.today's cse.js endpoint, which is not behind the access check.
    Results are cached permanently since snapshots never change. Returns None if there
    is no snapshot with that id."""
    if cached := _snapshots.get(short_id):
        return cached
    script = await _fetch_text(f"https://{CANONICAL_DOMAIN}/cse.js?id={short_id}", session)
    if script is None:
        raise RetrievalFailed(f"Archive.today did not answer the lookup of snapshot '{short_id}'.")
    snapshot = _parse_cse(short_id, script)
    if snapshot is None:
        return None
    _snapshots.put(short_id, snapshot)
    return snapshot


def _listing_rows(listing: str) -> list[tuple[str, str, str, str]]:
    """(short id, title, hash, capture time to the minute) of every capture in a listing."""
    return [(short_id, html_lib.unescape(title), content_hash, shown_at.strip())
            for short_id, title, content_hash, shown_at in LISTING_ROW_REGEX.findall(listing)]


async def _find_snapshot_by_time(original_url: str, captured_at: datetime,
                                 session: Optional[aiohttp.ClientSession]) -> Optional[Snapshot]:
    """Resolves a long-form snapshot URL (timestamp + original URL) to its short id via
    the capture listing that Archive.today shows for the original URL -- also not
    behind the access check. The listing shows capture times to the minute, which is
    what the match is made on."""
    listing = await _fetch_text(f"https://{CANONICAL_DOMAIN}/{original_url}", session)
    if not listing:
        return None
    wanted = captured_at.strftime("%d %b %Y %H:%M").lstrip("0")
    for short_id, title, _, shown_at in _listing_rows(listing):
        if shown_at == wanted:
            snapshot = await resolve_snapshot(short_id, session)
            if snapshot is not None:
                snapshot.title = title or None
                return snapshot
    return None


async def _fetch_title(snapshot: Snapshot, session: Optional[aiohttp.ClientSession]) -> Optional[str]:
    """The page title of a capture, taken from the (ungated) listing of its original URL."""
    listing = await _fetch_text(f"https://{CANONICAL_DOMAIN}/{snapshot.original_url}", session)
    if not listing:
        return None
    for short_id, title, _, _ in _listing_rows(listing):
        if short_id == snapshot.id:
            return title or None
    return None


async def identify_snapshot(url: str,
                            session: Optional[aiohttp.ClientSession] = None) -> Optional[Snapshot]:
    """Resolves any Archive.today snapshot URL -- short (/uTVE4) or long
    (/2022.05.05-091515/https://...) on any mirror -- to its Snapshot. Returns None if
    the URL does not point to a snapshot or the snapshot does not exist."""
    path = urlparse(url).path
    if match := SHORT_ID_REGEX.match(path):
        return await resolve_snapshot(match.group(1), session)
    if match := LONG_FORM_REGEX.match(path):
        if cached := _snapshots.get(path):
            return cached
        y, mo, d, h, mi, sec, original_url = match.groups()
        original_url = re.sub(r"^(https?:)/(?!/)", r"\1//", original_url)  # "https:/x" -> "https://x"
        captured_at = datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec), tzinfo=timezone.utc)
        snapshot = await _find_snapshot_by_time(original_url, captured_at, session)
        if snapshot is not None:
            _snapshots.put(path, snapshot)
        return snapshot
    return None


def _fallback_html(snapshot: Snapshot) -> str:
    """The content that is available without passing the access check: the capture's
    metadata and its full-page screenshot."""
    title = html_lib.escape(snapshot.title or snapshot.original_url)
    original = html_lib.escape(snapshot.original_url)
    captured_at = datetime.fromisoformat(snapshot.captured_at).strftime("%d %b %Y %H:%M:%S UTC")
    return (
        f"<html><body>"
        f"<h1>{title}</h1>"
        f'<p>Archive.today snapshot <a href="{snapshot.canonical_url}">{snapshot.id}</a> of '
        f'<a href="{original}">{original}</a>, captured {captured_at}.</p>'
        f"<p>Screenshot of the archived page:</p>"
        f'<img src="{snapshot.screenshot_url}" alt="Screenshot of {original}">'
        f"</body></html>"
    )


def _extract_content_html(page_html: str) -> Optional[str]:
    """Returns the markup of the snapshot's content div, or None if it is not present."""
    element = BeautifulSoup(page_html, "html.parser").find(id=CONTENT_DIV_ID)
    return str(element) if element else None


class ArchiveToday(HeadedBrowser):
    """Archive.today / archive.is / … retrieval.

    The replay page is fetched over plain HTTP with the stored session; a headed browser
    is used only to let a human pass the access check when there is no valid session
    (`HeadedBrowser` supplies that shared browser and the remote-view tunnel).
    """
    name = "Archive.today"
    # Every mirror is accepted as input, but all of them are served via CANONICAL_DOMAIN
    domains = [
        "archive.today",
        "archive.is",
        "archive.ph",
        "archive.vn",
        "archive.li",
        "archive.fo",
        "archive.md",
    ]

    # Serializes interactive solves so concurrent retrievals share one browser prompt
    _solve_lock: asyncio.Lock = asyncio.Lock()
    _remote_hint_shown: bool = False  # The remote-view instructions are logged only once

    @staticmethod
    def _clearance() -> Optional[str]:
        """The stored session's clearance cookie, or None if no session is stored."""
        for cookie in parse_cookies(get_secret("archive_today_cookie") or ""):
            if (cookie.get("name") == CLEARANCE_COOKIE
                    and cookie.get("domain", "").lstrip(".") == CANONICAL_DOMAIN):
                return cookie.get("value")
        return None

    @staticmethod
    async def _fetch_page(session: aiohttp.ClientSession, url: str) -> tuple[Optional[int], str]:
        """GETs a snapshot page with the stored clearance. Returns (status, body); tolerates
        the gate's 429 and a missing capture's 404 instead of raising on them."""
        clearance = ArchiveToday._clearance()
        headers = {"User-Agent": get_config_var("browser_user_agent") or "", **LOOKUP_HEADERS}
        cookies = {CLEARANCE_COOKIE: clearance} if clearance else None
        try:
            # archive.ph is in RELAXED_SSL_DOMAINS (archives serve mismatching certs), so
            # verification is off here as everywhere else in scrapeMM for these hosts.
            async with session.get(url, headers=headers, cookies=cookies,
                                   allow_redirects=True, ssl=False) as response:
                return response.status, await response.text()
        except Exception:
            logger.debug(f"Could not fetch Archive.today page {url}.", exc_info=True)
            return None, ""

    async def _get(self, url: str, **kwargs) -> ScrapedContent:
        """Retrieves the snapshot over plain HTTP. On the access check, solves it
        interactively (if enabled) or serves the screenshot fallback (if enabled)."""
        url = canonicalize_url(url)
        session: aiohttp.ClientSession = kwargs["session"]
        output_format = kwargs.get("output_format", "multimodal")

        content_html = await self._retrieve_content(session, url)
        if content_html is not None:
            return await to_scraped_content(content_html, session=session,
                                            output_format=output_format, url=url)

        # Still gated: fall back to the ungated screenshot and metadata, if enabled.
        if _screenshot_fallback_enabled():
            snapshot = await identify_snapshot(url, session)
            if snapshot is not None:
                logger.warning(f"⚠️ Archive.today page still gated; serving the screenshot and "
                               f"metadata of snapshot {snapshot.id} instead. {GATE_HINT}")
                return await self._screenshot_content(snapshot, session, output_format)

        raise CaptchaEncounteredError(GATE_HINT)

    async def _retrieve_content(self, session: aiohttp.ClientSession, url: str) -> Optional[str]:
        """The snapshot's content markup, or None if it stays behind the access check.
        Raises TargetUnavailableError if there is no such capture."""
        content, gated = await self._try_fetch_content(session, url)
        if content is not None:
            return content
        if not gated:
            raise TargetUnavailableError("Archive.today capture not found.")

        if not _interactive_solve_enabled():
            return None

        # Only one task solves at a time; the others re-use the session it establishes.
        async with self._solve_lock:
            content, gated = await self._try_fetch_content(session, url)
            if content is not None or not gated:
                return content
            if await self._solve_in_browser(url, _solve_timeout()):
                content, _ = await self._try_fetch_content(session, url)
                return content
        return None

    async def _try_fetch_content(self, session: aiohttp.ClientSession,
                                 url: str) -> tuple[Optional[str], bool]:
        """Fetches the page once. Returns (content markup or None, whether it was gated).

        Content presence is the discriminator: the access check is served with status 429
        and carries no content div, while a real snapshot page has one -- even when the
        archived text happens to contain a word like "captcha". So the content div is
        looked for first, and only its absence lets the gate markers speak.
        """
        status, body = await self._fetch_page(session, url)
        if content := _extract_content_html(body):
            return content, False
        if "not found (yet?)" in body.lower():
            return None, False
        return None, status == 429 or _looks_like_gate(body)

    @staticmethod
    async def _screenshot_content(snapshot: Snapshot, session: aiohttp.ClientSession,
                                  output_format: str) -> ScrapedContent:
        if snapshot.title is None:
            snapshot.title = await _fetch_title(snapshot, session)
            if snapshot.title:
                _snapshots.put(snapshot.id, snapshot)
        return await to_scraped_content(_fallback_html(snapshot), session=session,
                                        output_format=output_format, url=snapshot.canonical_url)

    async def _solve_in_browser(self, snapshot_url: str, timeout: float) -> bool:
        """Opens a snapshot in scrapeMM's browser so that a human can pass the access
        check, then stores the resulting session. Returns whether the check was passed."""
        from scrapemm.integrations.headed_browser import _resolve_profile_dir
        if _resolve_profile_dir() is None:
            logger.warning("⚠️ The browser is running without its persistent profile, so the "
                           "session you establish now will go stale much sooner. Close any "
                           "other running scrapeMM process to avoid that.")
        logger.info(f"🔒 Archive.today asks for a captcha. Opening {snapshot_url} in scrapeMM's "
                    f"browser — please pass the check (waiting up to {timeout:.0f}s).")

        async with async_playwright() as p:
            page, _ = await self._new_page(p)
            try:
                await page.goto(snapshot_url, timeout=60_000, wait_until="domcontentloaded")
                if not await self._await_gate_passed(page, timeout):
                    return False
                cookies = await page.context.cookies(f"https://{CANONICAL_DOMAIN}/")
                await self._pin_user_agent(page)
            except Exception:
                logger.warning("Archive.today session could not be established.", exc_info=True)
                return False
            finally:
                await page.close()

        self._store_cookies(cookies)
        return True

    async def capture_session(self, timeout: float = 300) -> bool:
        """Opens an Archive.today snapshot in scrapeMM's browser so that **you** can pass
        the access check, then stores the resulting session cookies.

        One session covers every mirror, since retrieval rewrites all of them to
        CANONICAL_DOMAIN. scrapeMM does not solve captchas: this only persists the session
        you established manually, which Archive.today binds to the browser that obtained
        it (hence this very window) and keeps valid for about five minutes.

        Returns whether the check was passed.
        """
        passed = await self._solve_in_browser(
            f"https://{CANONICAL_DOMAIN}/{VERIFICATION_SNAPSHOT}", timeout)
        if not passed:
            logger.warning("⚠️ No session established. Snapshot pages stay behind the access "
                           "check until you run this again.")
        return passed

    async def _await_gate_passed(self, page: Page, timeout: float) -> bool:
        """Waits until the page in front of us is no longer Archive.today's access check."""
        try:
            if not _looks_like_gate(await page.locator("body").inner_text()):
                logger.info("Already accessible, nothing to pass.")
                return True
        except Exception:
            pass

        if not self._remote_hint_shown and (hint := await remote_view_hint(page)):
            ArchiveToday._remote_hint_shown = True
            logger.info(hint)
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(2)
            try:
                body = await page.locator("body").inner_text()
            except Exception:
                continue  # Page is navigating; look again in a moment
            if body and not _looks_like_gate(body):
                return True

        logger.warning("⌛ Gave up waiting, the access check is still shown.")
        return False

    async def _pin_user_agent(self, page: Page) -> None:
        """Records the user agent the session was established with, so every later request
        presents the same one. Archive.today's session is tied to the browser that earned
        it, the user agent being the most visible part of that; the shared browser is
        Playwright's Chromium, whose user agent changes on every Playwright update, which
        would otherwise invalidate the stored session without any visible cause."""
        try:
            user_agent = await page.evaluate("navigator.userAgent")
        except Exception:
            logger.debug("Could not read the browser's user agent.", exc_info=True)
            return
        if user_agent and user_agent != get_config_var("browser_user_agent"):
            update_config(browser_user_agent=user_agent)
            logger.info(f"Pinned the browser user agent to: {user_agent}")

    def _store_cookies(self, cookies: list[dict]) -> None:
        """Persists the freshly captured session for the plain-HTTP retrieval to use."""
        set_secret("archive_today_cookie", json.dumps(cookies))
        has_clearance = any(c.get("name") == CLEARANCE_COOKIE for c in cookies)
        logger.info(f"✅ Stored {len(cookies)} Archive.today cookies"
                    f"{'' if has_clearance else ' (no clearance cookie — session may not work)'}.")


async def configure_archive_today_session(timeout: float = 300) -> bool:
    """Opens Archive.today in scrapeMM's browser so that you can pass its access check
    yourself, then stores the session for future retrievals. scrapeMM does not solve
    captchas — you do, once, in the window that opens."""
    from scrapemm.integrations import NAME_TO_INTEGRATION
    return await NAME_TO_INTEGRATION["archive.today"].capture_session(timeout=timeout)
