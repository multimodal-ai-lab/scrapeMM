"""Integration for Archive.today retrieval.

Archive.today gates every snapshot's replay page behind a Google reCAPTCHA (served with
HTTP 429). Passing it yields a `cf_clearance` cookie that unlocks the page for **exactly
five minutes** — activity does not extend it, and nothing but solving the CAPTCHA again
renews it (measured 2026-09-21). scrapeMM does not solve CAPTCHAs.

Five minutes is far less often than anyone will sit down to solve a CAPTCHA, so retrieval
is built around collecting work for the next session rather than waiting for one:

1. **Serve from the permanent page cache** if the snapshot was ever retrieved before. A
   capture never changes and only its replay page is gated, so one retrieval settles that
   URL for good -- no session needed again, ever.
2. **Otherwise fetch the replay page** with the stored `cf_clearance`. The fetch client is
   chosen once per process (`_ensure_fetch_mode`): the fast path is plain HTTP via curl_cffi
   browser-TLS impersonation with an aiohttp fallback (`_http_get`); where even that is
   served Archive.today's nginx decoy, retrieval falls back to the shared headed browser (a
   real Chromium it serves normally), which also resolves media in-frame. On the HTTP path
   the page's media lives on ungated `*.archive.ph` subdomains and downloads via curl_cffi
   too (see `BROWSER_TLS_DOMAINS` in `scrapemm.download.common`), so no session is needed.
3. **If the page is gated**, buffer the URL and fail immediately. The caller is not kept
   waiting for a human who may be hours away.
4. **When a session is established** (`capture_session()`), the whole buffer is retrieved
   and cached within those five minutes. One solved CAPTCHA therefore clears a batch, and
   every URL in it is answered from the cache from then on.

Two opt-ins sit beside this: `archive_today_interactive_solve` restores asking a human at
the moment of the request (sensible only for one-off, attended retrievals), and
`archive_today_screenshot_fallback` serves what Archive.today offers without the check --
the snapshot's full-page screenshot plus its original URL, capture time and title
(resolved through the ungated `cse.js` and capture-listing endpoints).
"""

import asyncio
import hashlib
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
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError

from scrapemm import CaptchaEncounteredError
from scrapemm.common.exceptions import RetrievalFailed, TargetUnavailableError
from scrapemm.common import get_config_var, update_config
from scrapemm.common.paths import CONFIG_DIR
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.download.common import HEADERS as _DEFAULT_HEADERS
from scrapemm.integrations.headed_browser import HeadedBrowser, ContentTarget, remote_view_hint
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

# Retrieved snapshot pages, likewise kept forever, and the URLs still waiting to be
# retrieved once a session exists
PAGE_CACHE_DIR = CONFIG_DIR / "archive_today_pages"
BUFFER_PATH = CONFIG_DIR / "archive_today_buffer.json"

# How many buffered pages are fetched at once. A session lasts only about five minutes,
# so the buffer has to be worked through briskly -- but not so briskly that the burst
# itself brings the gate back up.
DRAIN_CONCURRENCY = 4

# Whether a gated snapshot is opened in scrapeMM's browser for a human to solve the access
# check on the spot. Off by default: a session lasts about five minutes, so asking for a
# captcha at the moment of each request does not scale past a handful of URLs. Gated
# requests are buffered instead (see `_RequestBuffer`). Turn it back on for one-off,
# attended retrievals with `update_config(archive_today_interactive_solve=True)`.
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
#
# A browser User-Agent is essential: from some IPs (datacenter ranges especially),
# Archive.today serves a decoy page to non-browser agents like aiohttp's default
# "Python/aiohttp ...", while serving a normal browser string the real content. scrapeMM
# uses the same Firefox string as elsewhere, which Archive.today serves normally.
DEFAULT_USER_AGENT = _DEFAULT_HEADERS["User-Agent"]

# Archive.today serves an nginx "decoy" page to clients whose TLS fingerprint is not a
# real browser's -- aiohttp (Python's TLS) gets it on some IPs, a real browser or libcurl
# does not. So requests go through curl_cffi, which impersonates a browser's TLS. Several
# builds are tried because endpoints disagree: the capture listing is served to "chrome"
# / "safari" but 429s "chrome124", while cse.js and the replay page prefer "chrome124".
_IMPERSONATIONS = ("chrome124", "chrome", "safari")

ACCEPT_LANGUAGE = {"Accept-Language": "en-US,en;q=0.9"}


def _is_decoy(body: str) -> bool:
    return "welcome to nginx" in body.lower()


async def _get_via_curl_cffi(url: str, cookies: Optional[dict], user_agent: Optional[str],
                             impersonate: str, timeout: float) -> tuple[Optional[int], str]:
    """GET with a real browser's TLS fingerprint. This is what gets past the nginx decoy
    that Archive.today serves to non-browser clients like aiohttp on some IPs. A short
    timeout matters: Archive.today tarpits some requests (accepts the connection, sends
    nothing), and a browser fallback handles those, so waiting long only delays it."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return None, ""
    # No User-Agent override keeps curl_cffi's impersonation UA (a real browser build);
    # the gated page overrides it with the UA its session was pinned to.
    headers = {**ACCEPT_LANGUAGE, **({"User-Agent": user_agent} if user_agent else {})}
    try:
        async with AsyncSession() as session:
            response = await session.get(url, impersonate=impersonate, headers=headers,
                                         cookies=cookies, allow_redirects=True,
                                         verify=False, timeout=timeout)
            return response.status_code, response.text
    except Exception as e:
        logger.debug(f"curl_cffi ({impersonate}) GET failed for {url}: {type(e).__name__}.")
        return None, ""


async def _get_via_aiohttp(url: str, cookies: Optional[dict],
                           user_agent: Optional[str]) -> tuple[Optional[int], str]:
    """Plain-HTTP GET. Archive.today decoys this on some IPs but, oddly, serves it the
    capture listing that it 429s for curl_cffi -- hence both clients are tried."""
    headers = {"User-Agent": user_agent or DEFAULT_USER_AGENT, **ACCEPT_LANGUAGE}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, cookies=cookies,
                                   allow_redirects=True, ssl=False) as response:
                return response.status, await response.text()
    except Exception:
        logger.debug(f"aiohttp GET failed for {url}.", exc_info=True)
        return None, ""


async def _http_get(url: str, cookies: Optional[dict] = None, user_agent: Optional[str] = None,
                    impersonations: tuple[str, ...] = _IMPERSONATIONS,
                    timeout: float = 15) -> tuple[Optional[int], str]:
    """GETs an Archive.today URL resiliently. Archive.today's anti-bot is inconsistent
    across endpoints, clients and IPs: aiohttp is served an nginx decoy on some IPs, while
    curl_cffi's impersonation is 429'd on some endpoints for some builds. So the given
    browser impersonations and then aiohttp are tried; the first usable response (a 200 that
    is not the decoy) wins, failing which a real gate (429) is preferred over a decoy.
    Callers that hit a tarpit-prone endpoint (the replay page) pass a single impersonation
    so a stall costs one timeout, not one per build. Returns (status, body); (None, "") if
    nothing got through."""
    attempts: list[tuple[Optional[int], str]] = []
    for impersonate in impersonations:
        status, body = await _get_via_curl_cffi(url, cookies, user_agent, impersonate, timeout)
        if status == 200 and not _is_decoy(body):
            return status, body
        attempts.append((status, body))
    status, body = await _get_via_aiohttp(url, cookies, user_agent)
    if status == 200 and not _is_decoy(body):
        return status, body
    attempts.append((status, body))
    for status, body in attempts:  # no usable 200: a genuine gate beats a decoy
        if status == 429:
            return status, body
    return attempts[0] if attempts else (None, "")

GATE_HINT = (
    "Archive.today asks to solve a captcha. Cannot access the archived page's text. "
    "scrapeMM does not solve captchas. The URL was buffered: run "
    "scrapemm.configure_archive_today_session() to pass the check yourself once in "
    "scrapeMM's own browser, and everything buffered so far is retrieved and cached right "
    "after, so asking for it again succeeds without a session. Alternatively, "
    "update_config(archive_today_screenshot_fallback=True) makes scrapeMM serve the "
    "snapshot's screenshot and metadata instead of failing."
)


def _looks_like_gate(body_text: str) -> bool:
    """True if the page is Archive.today's access check rather than actual content."""
    return any(marker in body_text.lower() for marker in GATE_MARKERS)


def _blocked_hint(subject: str) -> str:
    """Message for the case where archive.today serves neither the expected page nor its
    access check nor a 'not found' page, but its nginx decoy / something unrecognized. The
    offending response itself is logged at WARNING level next to this message."""
    return (
        f"Archive.today served its nginx decoy or another unrecognized response instead of "
        f"{subject} -- not the content, not its access check, not a 'not found' page (the "
        f"response is logged at WARNING level). It does this to clients whose TLS "
        f"fingerprint is not a real browser's. scrapeMM already fetches via curl_cffi "
        f"browser impersonation to avoid this, so if it persists: make sure curl_cffi is "
        f"installed (it ships with scrapeMM), or the machine's IP may be blocked by "
        f"archive.today, or an intercepting proxy is rewriting the connection."
    )


def _interactive_solve_enabled() -> bool:
    return bool(get_config_var(INTERACTIVE_SOLVE_CONFIG_KEY, False))


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


def _page_cache_key(url: str) -> str:
    """Deterministic, filesystem-safe name for a canonicalized snapshot URL. Short-id
    URLs keep their id so that the store stays readable; long-form ones are hashed."""
    path = urlparse(url).path
    if match := SHORT_ID_REGEX.match(path):
        return match.group(1)
    return "u" + hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]


class _PageCache:
    """Permanently stored snapshot content, one file per snapshot.

    Only the replay page is behind the access check, and a capture never changes, so a
    page retrieved once never has to be retrieved again -- which is what lets a single
    solved captcha keep paying off long after its session expired. One file per snapshot
    keeps a new entry a single small write instead of a rewrite of the whole store.
    """

    def __init__(self, directory: Path = PAGE_CACHE_DIR):
        self.directory = directory

    def _file(self, url: str) -> Path:
        return self.directory / f"{_page_cache_key(url)}.html"

    def get(self, url: str) -> Optional[str]:
        try:
            return self._file(url).read_text(encoding="utf-8")
        except OSError:
            return None

    def put(self, url: str, content_html: str) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._file(url).write_text(content_html, encoding="utf-8")
        except OSError:
            logger.debug(f"Could not cache the Archive.today page for {url}.", exc_info=True)

    def __contains__(self, url: str) -> bool:
        return self._file(url).exists()

    def __len__(self) -> int:
        try:
            return len(list(self.directory.glob("*.html")))
        except OSError:
            return 0


class _RequestBuffer:
    """Snapshot URLs that were asked for while the access check was up.

    Waiting for a human at the moment of the request is hopeless for batch work -- the
    session lasts about five minutes, nobody solves a captcha that often. So a gated
    request fails right away and its URL is remembered here; the next time a session is
    established, everything that piled up is fetched in one go.
    """

    def __init__(self, path: Path = BUFFER_PATH):
        self.path = path
        self._urls: Optional[list[str]] = None

    def _load(self) -> list[str]:
        if self._urls is None:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self._urls = [u for u in loaded if isinstance(u, str)]
            except (OSError, ValueError):
                self._urls = []
        return self._urls

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._load(), indent=1), encoding="utf-8")
        except OSError:
            logger.debug(f"Could not persist the Archive.today buffer at {self.path}.",
                         exc_info=True)

    def urls(self) -> list[str]:
        """The buffered URLs, oldest first."""
        return list(self._load())

    def add(self, url: str) -> None:
        urls = self._load()
        if url in urls:
            return
        urls.append(url)
        self._save()
        logger.info(f"📥 Buffered {url} for the next Archive.today session "
                    f"({len(urls)} waiting).")

    def discard_many(self, done: set[str]) -> None:
        urls = self._load()
        remaining = [url for url in urls if url not in done]
        if len(remaining) != len(urls):
            self._urls = remaining
            self._save()

    def discard(self, url: str) -> None:
        self.discard_many({url})

    def clear(self) -> None:
        self._urls = []
        self._save()

    def __len__(self) -> int:
        return len(self._load())


_pages = _PageCache()
_buffer = _RequestBuffer()


async def _fetch_text(url: str, session: Optional[aiohttp.ClientSession] = None) -> Optional[str]:
    """Fetches an ungated Archive.today endpoint (cse.js, capture listing) as text, or None
    if it did not answer with 200. Goes through curl_cffi (see `_http_get`); the `session`
    argument is accepted for backward compatibility but no longer used, because a plain
    aiohttp session is the very thing Archive.today serves the decoy to."""
    status, body = await _http_get(url)
    return body if status == 200 and body else None


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
        raise RetrievalFailed(
            f"Archive.today did not answer the cse.js lookup of snapshot '{short_id}' "
            f"(no response or a non-200 status). " + _blocked_hint("its cse.js lookup endpoint"))
    # Both a real hit and the stub for a non-existent id carry this id-specific marker;
    # its absence means the response is not cse.js at all (a decoy, block or DNS junk).
    if f"cse-serp-thumb-{short_id}" not in script:
        logger.warning("Archive.today served a non-cse.js response for id %r (%d chars): %r",
                       short_id, len(script), " ".join(script.split())[:200])
        raise RetrievalFailed(_blocked_hint("its cse.js lookup endpoint"))
    snapshot = _parse_cse(short_id, script)
    if snapshot is None:
        return None  # genuine: cse.js served the data-less stub for a non-existent id
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


# Outcome of fetching a snapshot page: real content, the access check, a genuine missing
# capture, or an unrecognized response that means the network cannot reach archive.today.
CONTENT, GATE, NOT_FOUND, BLOCKED = "content", "gate", "notfound", "blocked"

# How this machine reaches Archive.today, decided once by probing (see `_ensure_fetch_mode`):
# FETCH_HTTP uses the fast curl_cffi/aiohttp path; FETCH_BROWSER routes retrieval through
# the shared headed browser because plain HTTP is served the decoy here.
FETCH_HTTP, FETCH_BROWSER = "http", "browser"


class _HttpUnusable(RetrievalFailed):
    """Raised on the HTTP path when Archive.today returns a page it cannot use -- a decoy,
    or a JavaScript-rendered variant that only a real browser resolves. The caller retries
    that one URL through the browser; if it escapes uncaught it is still a RetrievalFailed."""


def _raise_for_state(state: str) -> None:
    """Raises the fitting error for a terminal non-content outcome; a no-op for GATE (which
    the caller handles by solving) and CONTENT."""
    if state == NOT_FOUND:
        raise TargetUnavailableError("Archive.today has no capture at this URL.")
    if state == BLOCKED:
        raise _HttpUnusable(_blocked_hint("the archived page"))


class ArchiveToday(HeadedBrowser):
    """Archive.today / archive.is / … retrieval.

    The replay page is fetched over plain HTTP (curl_cffi/aiohttp, see `_http_get`) where
    that works, or through the shared headed browser where Archive.today decoys plain HTTP;
    which one is decided once by `_ensure_fetch_mode()`. The browser (from `HeadedBrowser`)
    is used in either case to let a human pass the access check, via the remote-view tunnel.
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

    # Set once by `_ensure_fetch_mode()`; all later retrievals follow it with no re-probe.
    _fetch_mode: Optional[str] = None
    _fetch_mode_lock: asyncio.Lock = asyncio.Lock()

    async def _ensure_fetch_mode(self) -> str:
        """Decides once whether this machine can reach Archive.today over plain HTTP or must
        go through the browser (because HTTP is served the decoy here), caches the verdict on
        the class, and returns it. Subsequent calls return the cached verdict without probing."""
        if ArchiveToday._fetch_mode is not None:
            return ArchiveToday._fetch_mode
        async with self._fetch_mode_lock:
            if ArchiveToday._fetch_mode is None:
                ArchiveToday._fetch_mode = await self._probe_fetch_mode()
            return ArchiveToday._fetch_mode

    async def _probe_fetch_mode(self) -> str:
        """Probes the ungated cse.js endpoint over the HTTP path. A trusted client gets the
        real script; a decoyed one gets the nginx page. Anything but a clean hit means HTTP
        is unreliable here, so retrieval goes through the browser."""
        status, body = await _http_get(
            f"https://{CANONICAL_DOMAIN}/cse.js?id={VERIFICATION_SNAPSHOT}")
        if status == 200 and f"cse-serp-thumb-{VERIFICATION_SNAPSHOT}" in body:
            logger.info("Archive.today reachable over plain HTTP; using the fast path.")
            return FETCH_HTTP
        logger.info("Archive.today serves this machine the decoy over plain HTTP; routing "
                    "retrieval through the browser instead.")
        return FETCH_BROWSER

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
        """GETs a snapshot page with the stored clearance, through curl_cffi (see
        `_http_get`). Returns (status, body); the gate's 429 and a missing capture's 404
        come back as data, not exceptions. The `session` argument is unused -- a plain
        aiohttp session is exactly what Archive.today serves the decoy to."""
        clearance = ArchiveToday._clearance()
        cookies = {CLEARANCE_COOKIE: clearance} if clearance else None
        # The clearance is bound to the user agent its session was solved with, so present
        # that exact one when it is known; otherwise curl_cffi's own browser UA is fine.
        # One impersonation only: the replay page is the endpoint Archive.today tarpits, so
        # trying every build would multiply the stall; a JS-rendered page falls to the
        # browser regardless.
        return await _http_get(url, cookies=cookies,
                               user_agent=get_config_var("browser_user_agent") or None,
                               impersonations=("chrome124",))

    async def _get(self, url: str, **kwargs) -> ScrapedContent:
        """Retrieves the snapshot. The fetch client (plain HTTP vs. the browser) is chosen
        once per process by `_ensure_fetch_mode()`; on the access check the check is solved
        interactively (if enabled) or the screenshot fallback is served (if enabled)."""
        url = canonicalize_url(url)
        session: aiohttp.ClientSession = kwargs["session"]
        output_format = kwargs.get("output_format", "multimodal")

        # A capture never changes and only its page is gated, so a page retrieved once
        # serves every later request -- no session needed.
        if cached := _pages.get(url):
            logger.debug(f"Serving the cached Archive.today page for {url}.")
            return await to_scraped_content(cached, session=session,
                                            output_format=output_format, url=url)

        if await self._ensure_fetch_mode() == FETCH_BROWSER:
            return await self._get_via_browser(url, **kwargs)

        try:
            content_html = await self._retrieve_content(session, url)
        except _HttpUnusable:
            # This capture's HTTP response is not usable (a decoy, or a JS-rendered page);
            # the browser renders it even though the fast path could not.
            logger.info(f"Plain HTTP could not render {url}; falling back to the browser for it.")
            return await self._get_via_browser(url, **kwargs)
        if content_html is not None:
            _pages.put(url, content_html)
            _buffer.discard(url)
            return await to_scraped_content(content_html, session=session,
                                            output_format=output_format, url=url)

        # Gated. Remember the URL so the next session picks it up, and do not make the
        # caller wait for a human: five-minute sessions make that pointless at any scale.
        _buffer.add(url)

        # Fall back to the ungated screenshot and metadata, if enabled. Not cached: it is
        # a stand-in, and caching it would keep the real page from ever being served.
        if _screenshot_fallback_enabled():
            snapshot = await identify_snapshot(url, session)
            if snapshot is not None:
                logger.warning(f"⚠️ Archive.today page still gated; serving the screenshot and "
                               f"metadata of snapshot {snapshot.id} instead. {GATE_HINT}")
                return await self._screenshot_content(snapshot, session, output_format)

        raise CaptchaEncounteredError(GATE_HINT)

    async def _get_via_browser(self, url: str, **kwargs) -> ScrapedContent:
        """Browser fetch path, used when plain HTTP is decoyed on this machine or cannot
        render a particular capture. The shared headed browser (a real Chromium, which
        Archive.today serves normally) opens the snapshot, `_extract_content` pulls out its
        content div, and media is resolved in-frame. On the access check it is solved
        interactively, once, and shared across concurrent retrievals. Not page-cached:
        re-serving would re-resolve media over HTTP, so each URL is fetched afresh here."""
        try:
            return await super()._get(url, **kwargs)
        except CaptchaEncounteredError:
            if _interactive_solve_enabled():
                async with self._solve_lock:
                    try:
                        return await super()._get(url, **kwargs)  # another task may have solved
                    except CaptchaEncounteredError:
                        pass
                    if await self._solve_in_browser(url, _solve_timeout()):
                        return await super()._get(url, **kwargs)
            raise CaptchaEncounteredError(GATE_HINT)

    async def _extract_content(self, page: Page) -> Optional[ContentTarget]:
        """Browser-path content extraction: returns the snapshot's content div, or raises
        for the access check / a missing capture. (Only reached in FETCH_BROWSER mode; the
        HTTP path classifies the page itself.)"""
        try:
            body_text = (await page.locator("body").inner_text()).lower()
        except Exception:
            body_text = ""
        if "not found (yet?)" in body_text:
            raise TargetUnavailableError("Archive.today has no capture at this URL.")
        if _looks_like_gate(body_text):
            raise CaptchaEncounteredError(GATE_HINT)
        try:
            element = await page.wait_for_selector(f"#{CONTENT_DIV_ID}", timeout=30000)
            if element:
                return element
        except (TimeoutError, PlaywrightTimeoutError):
            logger.warning("Archive.today #%s missing at '%s'.", CONTENT_DIV_ID, page.url)
        return None

    async def _retrieve_content(self, session: aiohttp.ClientSession, url: str) -> Optional[str]:
        """The snapshot's content markup, or None if it stays behind the access check.
        Raises TargetUnavailableError if there is no such capture."""
        content, state = await self._try_fetch_content(session, url)
        if content is not None:
            return content
        _raise_for_state(state)  # raises for NOT_FOUND / BLOCKED; returns for GATE

        if not _interactive_solve_enabled():
            return None

        # Only one task solves at a time; the others re-use the session it establishes.
        async with self._solve_lock:
            content, state = await self._try_fetch_content(session, url)
            if content is not None:
                return content
            _raise_for_state(state)
            if await self._solve_in_browser(url, _solve_timeout()):
                content, state = await self._try_fetch_content(session, url)
                if content is None:
                    _raise_for_state(state)  # a BLOCKED response after solving still raises
                    return None  # still gated: solving did not take
                # The session is live now and short-lived: spend it on the backlog too
                await self._drain_buffer(session, skip={url})
                return content
        return None

    async def _drain_buffer(self, session: aiohttp.ClientSession,
                            skip: Optional[set[str]] = None) -> int:
        """Retrieves everything the buffer holds and caches it. Returns how many pages
        were fetched.

        Called right after a session is established, because that session lasts about
        five minutes -- so this is the one window in which the backlog can be cleared.
        If the gate comes back mid-way (the session expired, or the burst itself tripped
        it), what is left stays buffered for the next round.
        """
        urls = [url for url in _buffer.urls() if url not in (skip or set())]
        if not urls:
            return 0

        logger.info(f"📤 Retrieving {len(urls)} buffered Archive.today page(s)...")
        semaphore = asyncio.Semaphore(DRAIN_CONCURRENCY)
        done: set[str] = set()
        needs_browser: list[str] = []  # HTTP couldn't render these (JS-rendered captures)
        gated_again = False

        async def fetch(url: str) -> None:
            nonlocal gated_again
            if gated_again:
                return
            async with semaphore:
                if gated_again:
                    return
                try:
                    content, state = await self._try_fetch_content(session, url)
                except Exception:
                    logger.debug(f"Buffered Archive.today page {url} could not be "
                                 f"retrieved.", exc_info=True)
                    return
                if content is not None:
                    _pages.put(url, content)
                    done.add(url)
                elif state == GATE:
                    gated_again = True
                elif state == NOT_FOUND:
                    # No such capture -- retrying it in every future round is pointless
                    logger.debug(f"Dropping {url} from the buffer: no such capture.")
                    done.add(url)
                else:  # BLOCKED: the HTTP path can't render this one; the browser can
                    needs_browser.append(url)

        await asyncio.gather(*(fetch(url) for url in urls))

        # The session is still live in the browser, so render the pages HTTP could not.
        # Sequentially -- there is one shared browser. output_format="html" caches the
        # markup; media resolves on serve.
        if needs_browser and not gated_again:
            logger.info(f"🌐 {len(needs_browser)} page(s) need the browser to render; "
                        f"retrieving them through it...")
        for url in needs_browser:
            if gated_again:
                break
            try:
                content = await super()._get(url, session=session, output_format="html")
            except CaptchaEncounteredError:
                gated_again = True  # session expired mid-drain
            except TargetUnavailableError:
                done.add(url)  # no such capture -- stop retrying it
            except Exception:
                logger.debug(f"Buffered Archive.today page {url} could not be retrieved "
                             f"via the browser.", exc_info=True)
            else:
                if content and content.html:
                    _pages.put(url, content.html)
                    done.add(url)

        _buffer.discard_many(done)

        if gated_again:
            logger.warning(f"⚠️ Archive.today asks for a captcha again after "
                           f"{len(done)} page(s); {len(_buffer)} still buffered. Solve it "
                           f"once more to continue.")
        else:
            logger.info(f"✅ Cached {len(done)} Archive.today page(s). They are served "
                        f"from the cache from now on, no session needed.")
        return len(done)

    async def _try_fetch_content(self, session: aiohttp.ClientSession,
                                 url: str) -> tuple[Optional[str], str]:
        """Fetches the page once. Returns (content markup or None, one of CONTENT / GATE /
        NOT_FOUND / BLOCKED).

        Content presence is the discriminator: the access check is served with status 429
        and carries no content div, while a real snapshot page has one -- even when the
        archived text happens to contain a word like "captcha". So the content div is
        looked for first, and only its absence lets the gate markers speak. A response that
        is none of these is BLOCKED: archive.today could not be reached cleanly (decoy,
        block or DNS junk), which must not be mistaken for a missing capture.
        """
        status, body = await self._fetch_page(session, url)
        if content := _extract_content_html(body):
            return content, CONTENT
        if "not found (yet?)" in body.lower():
            return None, NOT_FOUND
        if status == 429 or _looks_like_gate(body):
            return None, GATE
        logger.warning("Archive.today served an unrecognized response for %s (status %s, "
                       "%d chars): %r", url, status, len(body), " ".join(body.split())[:200])
        return None, BLOCKED

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

        Every URL that was requested while the check was up is then retrieved and cached
        on the spot, so that those five minutes buy more than a single page.

        Returns whether the check was passed.
        """
        passed = await self._solve_in_browser(
            f"https://{CANONICAL_DOMAIN}/{VERIFICATION_SNAPSHOT}", timeout)
        if not passed:
            logger.warning("⚠️ No session established. Snapshot pages stay behind the access "
                           "check until you run this again.")
            return False

        async with aiohttp.ClientSession() as session:
            await self._drain_buffer(session)
        return True

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
    captchas — you do, once, in the window that opens.

    Everything that was requested while the check was up is retrieved and cached right
    after, so one solved captcha clears the whole backlog."""
    from scrapemm.integrations import NAME_TO_INTEGRATION
    return await NAME_TO_INTEGRATION["archive.today"].capture_session(timeout=timeout)


def get_archive_today_buffer() -> list[str]:
    """The snapshot URLs that were requested while the access check was up, oldest first.
    They are retrieved and cached the next time a session is established."""
    return _buffer.urls()


def clear_archive_today_buffer() -> None:
    """Forgets every buffered snapshot URL without retrieving it."""
    _buffer.clear()
    logger.info("Cleared the Archive.today buffer.")


async def retrieve_buffered_archive_today() -> int:
    """Retrieves and caches every buffered snapshot URL using the session that is already
    stored, without asking for a new captcha. Returns how many pages were cached.

    `configure_archive_today_session()` does this for you; use this directly only to
    resume a drain that the gate interrupted while the session is still valid."""
    from scrapemm.integrations import NAME_TO_INTEGRATION
    integration = NAME_TO_INTEGRATION["archive.today"]
    async with aiohttp.ClientSession() as session:
        return await integration._drain_buffer(session)


def count_cached_archive_today_pages() -> int:
    """How many Archive.today pages are held in the permanent cache."""
    return len(_pages)
