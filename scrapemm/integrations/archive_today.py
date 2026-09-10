"""Integration for Archive.today retrieval."""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Page

from scrapemm import CaptchaEncounteredError
from scrapemm.common.exceptions import (RetrievalFailed, TargetUnavailableError,
                                        AccessBlockedError)
from scrapemm.integrations.headed_browser import HeadedBrowser, ContentTarget, remote_view_hint
from scrapemm.secrets import get_secret, set_secret
from scrapemm.util import parse_cookies, parse_netscape_cookies

logger = logging.getLogger("scrapeMM")

ARCHIVE_TODAY_CONTENT_DIV_ID = "CONTENT"

# Session cookies that help to bypass Archive.today's CAPTCHA gate. They expire
# eventually, in which case you can supply your own ones by setting the
# 'archive_today_cookie' secret, see `scrapemm.configure_secrets()`.
DEFAULT_COOKIES_PATH = Path(__file__).parent / "archive_today_cookies.txt"

# Instead of the snapshot, Archive.today serves nginx' default page ("Welcome to nginx!")
# to clients it distrusts. It is served per browser build: at the time of writing, every
# Chrome 153 gets it (manual browsing included) while Playwright's bundled Chromium and
# Firefox are served normally, no matter their cookies, IP address or request headers.
# That is why the shared browser runs on Playwright's Chromium, see
# `headed_browser._resolve_browser_executable()`. Unlike the CAPTCHA gate, the decoy page
# sets no session cookie, so retrying never gets us out of it.
DECOY_HINT = (
    "Archive.today served nginx' default page instead of the snapshot. It does that for "
    "browser builds it distrusts (e.g. the very latest Chrome release). Make sure "
    "Playwright's Chromium is installed ('playwright install'), since scrapeMM prefers it "
    "over the locally installed Chrome for exactly this reason. You can point scrapeMM at "
    "another browser binary via update_config(browser_executable_path=...)."
)


# Markers of Archive.today's access check. It is shown both as a bot challenge and as
# the body of a 429 response when the IP address sent too many requests recently.
GATE_MARKERS = ("security check", "captcha", "just a moment", "performing security verification")


def _looks_like_gate(body_text: str) -> bool:
    """True if the page is Archive.today's access check rather than actual content."""
    return any(marker in body_text.lower() for marker in GATE_MARKERS)


def _session_cookies(cookies: list[dict]) -> list[dict]:
    """Return cookies without expires so Chromium treats them as session cookies."""
    return [{k: v for k, v in cookie.items() if k != "expires"} for cookie in cookies]


# Archive.today serves its access check on every snapshot URL — including ids that do not
# exist, and regardless of the client's IP address — with status 429, while its landing
# page stays at 200. So the status says "too many requests", but what it actually demands
# is a session that passed the check.
GATE_HINT = (
    "Archive.today asks to solve a captcha. Cannot access archived content. scrapeMM does "
    "not solve captchas. Run scrapemm.configure_archive_today_session() to pass the check "
    "yourself once in scrapeMM's own browser, for each mirror domain you need; the "
    "resulting session is then re-used."
)

# The session has to be established on the kind of URL that retrieval uses: a snapshot
# referenced by its short id. Archive.today keeps its landing page and some other URL
# forms accessible even while it gates those, so checking anything else gives a false
# sense of access. Which id is used does not matter — the access check precedes the
# lookup, so even an unknown id brings it up.
VERIFICATION_SNAPSHOT = "Ubqsd"


def _mirror_to_uncovered_domains(cookies: list[dict], domains: list[str]) -> list[dict]:
    """Archive.today is reachable under several mirror domains but cookies are usually
    only available for the one the user (or we) visited. So copy them over to every
    mirror domain that has no cookies of its own."""
    if not cookies:
        return cookies

    covered = {cookie.get("domain", "").lstrip(".") for cookie in cookies}
    first_domain = cookies[0].get("domain", "").lstrip(".")
    template = [cookie for cookie in cookies
                if cookie.get("domain", "").lstrip(".") == first_domain]

    mirrored = list(cookies)
    for domain in domains:
        if domain in covered:
            continue
        for cookie in template:
            if cookie["name"] == "cf_clearance":
                # Cloudflare binds its clearance to one domain; a copy is worthless
                continue
            copy = dict(cookie)
            copy["domain"] = ("." if cookie["domain"].startswith(".") else "") + domain
            mirrored.append(copy)
    return mirrored


class ArchiveToday(HeadedBrowser):
    """Archive.today / archive.is / … via UC headed Chromium.

    Headless Playwright + stealth used to work, but recent Playwright builds default
    to chrome-headless-shell, which Archive.today often blocks on Linux (no #CONTENT).
    The headed UC stack already works for the other archive integrations on that host.
    """
    name = "Archive.today"
    domains = [
        "archive.today",
        "archive.is",
        "archive.ph",
        "archive.vn",
        "archive.li",
        "archive.fo",
        "archive.md",
    ]

    _cookies: Optional[list[dict]] = None
    _remote_hint_shown: bool = False  # The remote-view instructions are logged only once

    def _load_cookies(self) -> list[dict]:
        """Returns the cookies to use, preferring the user-configured ones over the
        defaults shipped with scrapeMM. Configure your own cookies by exporting them
        from your browser (cookies.txt or JSON) and running
        `scrapemm.override_secret("archive_today_cookie")`."""
        if self._cookies is None:
            configured = get_secret("archive_today_cookie")
            cookies = parse_cookies(configured) if configured else []
            if cookies:
                logger.debug(f"Using {len(cookies)} configured Archive.today cookies.")
            else:
                if configured:
                    logger.warning("⚠️ Could not parse the configured Archive.today cookies. "
                                   "Falling back to the default ones.")
                cookies = parse_netscape_cookies(DEFAULT_COOKIES_PATH)
            ArchiveToday._cookies = _mirror_to_uncovered_domains(cookies, self.domains)
        return self._cookies

    async def _prepare_context(self, context) -> None:
        try:
            await context.add_cookies(_session_cookies(self._load_cookies()))
        except Exception:
            logger.warning("Could not add Archive.today cookies to browser context.", exc_info=True)

    async def capture_session(self, timeout: float = 300,
                              domains: Optional[list[str]] = None) -> bool:
        """Opens a snapshot of every Archive.today mirror in scrapeMM's browser so that
        **you** can pass their access checks, then stores the resulting session cookies.

        Every mirror is a separate Cloudflare zone that hands out its own clearance
        cookie, so the check has to be passed once per mirror — the ones you skip stay
        unavailable. `timeout` applies per mirror. Cookies of mirrors that were captured
        earlier are kept, so you can do this in several goes.

        scrapeMM does not solve captchas: this only persists the sessions you established
        manually. Cloudflare binds each clearance to the browser and IP address that
        obtained it, which is why the checks have to be passed in this very browser window.

        Returns True if every requested mirror serves snapshots afterwards.
        """
        domains = domains or self.domains
        captured: dict[str, list[dict]] = {}

        logger.info(f"Establishing Archive.today sessions for {len(domains)} mirrors: "
                    f"{', '.join(domains)}")

        async with async_playwright() as p:
            page, _ = await self._new_page(p)
            try:
                for i, domain in enumerate(domains, start=1):
                    url = f"https://{domain}/{VERIFICATION_SNAPSHOT}"
                    prefix = f"[{i}/{len(domains)}] {domain}"
                    try:
                        await page.goto(url, timeout=60_000, wait_until="domcontentloaded")
                    except Exception as e:
                        logger.warning(f"{prefix}: could not be opened ({type(e).__name__}).")
                        continue

                    if await self._await_gate_passed(page, prefix, timeout):
                        captured[domain] = await page.context.cookies(f"https://{domain}/")
                        logger.info(f"{prefix}: ✅ {len(captured[domain])} cookies captured.")
            finally:
                await page.close()

        if captured:
            self._store_cookies(captured)

        missing = [domain for domain in domains if domain not in captured]
        if missing:
            logger.warning(f"⚠️ No session for: {', '.join(missing)}. Snapshots on those "
                           f"mirrors stay unavailable until you run this again for them.")
        return not missing

    async def _await_gate_passed(self, page: Page, prefix: str, timeout: float) -> bool:
        """Waits until the page in front of us is no longer Archive.today's access check."""
        try:
            if not _looks_like_gate(await page.locator("body").inner_text()):
                logger.info(f"{prefix}: already accessible, nothing to pass.")
                return True
        except Exception:
            pass

        logger.info(f"{prefix}: 👉 please pass the access check in the browser window "
                    f"(waiting up to {timeout:.0f}s)...")
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

        logger.warning(f"{prefix}: ⌛ gave up waiting, the access check is still shown.")
        return False

    def _store_cookies(self, captured: dict[str, list[dict]]) -> None:
        """Merges the freshly captured cookies into the stored session, keeping the ones
        of mirrors that were not part of this run."""
        kept = [cookie for cookie in parse_cookies(get_secret("archive_today_cookie") or "")
                if cookie.get("domain", "").lstrip(".") not in captured]
        cookies = kept + [cookie for domain_cookies in captured.values()
                          for cookie in domain_cookies]
        set_secret("archive_today_cookie", json.dumps(cookies))
        ArchiveToday._cookies = None  # Re-read on the next retrieval
        logger.info(f"Stored {len(cookies)} Archive.today cookies covering "
                    f"{len(captured)} mirror(s). They are used automatically from now on.")

    async def _extract_content(self, page: Page) -> Optional[ContentTarget]:
        try:
            body_text = (await page.locator("body").inner_text()).lower()
        except Exception:
            logger.debug("Could not read Archive.today body text", exc_info=True)
            body_text = ""

        # Catch missing capture
        if "Not Found (yet?)".lower() in body_text.lower():
            raise TargetUnavailableError("Archive.today capture not found.")

        # Detect the decoy page, see DECOY_HINT
        if "welcome to nginx" in body_text:
            raise RetrievalFailed(DECOY_HINT)

        # Detect the access check
        if _looks_like_gate(body_text):
            raise CaptchaEncounteredError(GATE_HINT)

        # CAPTCHA gate sometimes redirects to the bare host with no snapshot path.
        if urlparse(page.url).path in ("", "/"):
            raise AccessBlockedError("Archive.today redirects to a decoy page.")

        try:
            element = await page.wait_for_selector(f"#{ARCHIVE_TODAY_CONTENT_DIV_ID}", timeout=30000)
            if element:
                return element
        except (TimeoutError, PlaywrightTimeoutError):
            # Catch both: builtin TimeoutError and Playwright's (unrelated) TimeoutError class.
            snippet = body_text[:240].replace("\n", " ")
            logger.warning(
                "Archive.today #%s missing at '%s'. Body starts with: %r",
                ARCHIVE_TODAY_CONTENT_DIV_ID, page.url, snippet,
            )
        return None


async def configure_archive_today_session(timeout: float = 300) -> bool:
    """Opens Archive.today in scrapeMM's browser so that you can pass its access check
    yourself, then stores the session for future retrievals. scrapeMM does not solve
    captchas — you do, once, in the window that opens."""
    from scrapemm.integrations import NAME_TO_INTEGRATION
    return await NAME_TO_INTEGRATION["archive.today"].capture_session(timeout=timeout)
