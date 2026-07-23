"""Integration for Archive.today retrieval."""

import logging
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, Page

from scrapemm.common.exceptions import TargetUnavailableError
from scrapemm.integrations.headed_browser import HeadedBrowser, ContentTarget

logger = logging.getLogger("scrapeMM")

ARCHIVE_TODAY_CONTENT_DIV_ID = "CONTENT"

# Captured session cookies that help bypass Archive.today's CAPTCHA gate.
# `expires` is stripped at use-time so Chromium still accepts them as session cookies
# after the recorded expiry timestamps have passed.
COOKIES = [
    # --- archive.is ---
    {"name": "qki", "value": "899095247488898009", "domain": ".archive.is", "path": "/",
     "expires": 1776116330, "httpOnly": False, "secure": False, "sameSite": "Lax"},
    {"name": "HstCfa2293961", "value": "1776109957355", "domain": "archive.is", "path": "/",
     "expires": 1807645957, "httpOnly": False, "secure": False, "sameSite": "Lax"},
    {"name": "HstCla2293961", "value": "1776112901839", "domain": "archive.is", "path": "/",
     "expires": 1807648901, "httpOnly": False, "secure": False, "sameSite": "Lax"},
    {"name": "HstCmu2293961", "value": "1776109957355", "domain": "archive.is", "path": "/",
     "expires": 1807645957, "httpOnly": False, "secure": False, "sameSite": "Lax"},
    {"name": "HstPn2293961", "value": "4", "domain": "archive.is", "path": "/", "expires": 1807648901,
     "httpOnly": False, "secure": False, "sameSite": "Lax"},
    {"name": "HstPt2293961", "value": "4", "domain": "archive.is", "path": "/", "expires": 1807648901,
     "httpOnly": False, "secure": False, "sameSite": "Lax"},
    {"name": "HstCnv2293961", "value": "1", "domain": "archive.is", "path": "/", "expires": 1807648901,
     "httpOnly": False, "secure": False, "sameSite": "Lax"},
    {"name": "HstCns2293961", "value": "2", "domain": "archive.is", "path": "/", "expires": 1807648901,
     "httpOnly": False, "secure": False, "sameSite": "Lax"},

    # --- archive.ph ---
    {"domain": "archive.ph", "path": "/", "name": "HstCfa2293961", "value": "1775752561306", "expires": 1807288561,
     "httpOnly": False, "secure": False},
    {"domain": "archive.ph", "path": "/", "name": "HstCla2293961", "value": "1776114941713", "expires": 1807650941,
     "httpOnly": False, "secure": False},
    {"domain": "archive.ph", "path": "/", "name": "HstCmu2293961", "value": "1775752561306", "expires": 1807288561,
     "httpOnly": False, "secure": False},
    {"domain": "archive.ph", "path": "/", "name": "HstPn2293961", "value": "3", "expires": 1807650941,
     "httpOnly": False, "secure": False},
    {"domain": "archive.ph", "path": "/", "name": "HstPt2293961", "value": "4", "expires": 1807650941,
     "httpOnly": False, "secure": False},
    {"domain": "archive.ph", "path": "/", "name": "HstCnv2293961", "value": "2", "expires": 1807650941,
     "httpOnly": False, "secure": False},
    {"domain": "archive.ph", "path": "/", "name": "HstCns2293961", "value": "3", "expires": 1807650941,
     "httpOnly": False, "secure": False},
    {"domain": ".archive.ph", "path": "/", "name": "qki", "value": "5916913656267410838", "expires": 1776118541,
     "httpOnly": False, "secure": False},

    # --- archive.vn ---
    {"domain": ".archive.vn", "path": "/", "name": "qki", "value": "17207459285007075470", "expires": 1776118630,
     "httpOnly": False, "secure": False},
    {"domain": "archive.vn", "path": "/", "name": "HstCfa2293961", "value": "1776115023034", "expires": 1807651023,
     "httpOnly": False, "secure": False},
    {"domain": "archive.vn", "path": "/", "name": "HstCla2293961", "value": "1776115030131", "expires": 1807651030,
     "httpOnly": False, "secure": False},
    {"domain": "archive.vn", "path": "/", "name": "HstCmu2293961", "value": "1776115023034", "expires": 1807651023,
     "httpOnly": False, "secure": False},
    {"domain": "archive.vn", "path": "/", "name": "HstPn2293961", "value": "2", "expires": 1807651030,
     "httpOnly": False, "secure": False},
    {"domain": "archive.vn", "path": "/", "name": "HstPt2293961", "value": "2", "expires": 1807651030,
     "httpOnly": False, "secure": False},
    {"domain": "archive.vn", "path": "/", "name": "HstCnv2293961", "value": "1", "expires": 1807651030,
     "httpOnly": False, "secure": False},
    {"domain": "archive.vn", "path": "/", "name": "HstCns2293961", "value": "1", "expires": 1807651030,
     "httpOnly": False, "secure": False},

    # --- archive.fo ---
    {"domain": ".archive.fo", "path": "/", "name": "qki", "value": "4449720061710956499", "expires": 1776118647,
     "httpOnly": False, "secure": False},
    {"domain": "archive.fo", "path": "/", "name": "HstCfa2293961", "value": "1776115047204", "expires": 1807651047,
     "httpOnly": False, "secure": False},
    {"domain": "archive.fo", "path": "/", "name": "HstCla2293961", "value": "1776115047204", "expires": 1807651047,
     "httpOnly": False, "secure": False},
    {"domain": "archive.fo", "path": "/", "name": "HstCmu2293961", "value": "1776115047204", "expires": 1807651047,
     "httpOnly": False, "secure": False},
    {"domain": "archive.fo", "path": "/", "name": "HstPn2293961", "value": "1", "expires": 1807651047,
     "httpOnly": False, "secure": False},
    {"domain": "archive.fo", "path": "/", "name": "HstPt2293961", "value": "1", "expires": 1807651047,
     "httpOnly": False, "secure": False},
    {"domain": "archive.fo", "path": "/", "name": "HstCnv2293961", "value": "1", "expires": 1807651047,
     "httpOnly": False, "secure": False},
    {"domain": "archive.fo", "path": "/", "name": "HstCns2293961", "value": "1", "expires": 1807651047,
     "httpOnly": False, "secure": False},

    # --- archive.md ---
    {"domain": ".archive.md", "path": "/", "name": "qki", "value": "6722863595999379080", "expires": 1776122883,
     "httpOnly": False, "secure": False},
    {"domain": "archive.md", "path": "/", "name": "HstCfa2293961", "value": "1776119275554", "expires": 1807655275,
     "httpOnly": False, "secure": False},
    {"domain": "archive.md", "path": "/", "name": "HstCla2293961", "value": "1776119275554", "expires": 1807655275,
     "httpOnly": False, "secure": False},
    {"domain": "archive.md", "path": "/", "name": "HstCmu2293961", "value": "1776119275554", "expires": 1807655275,
     "httpOnly": False, "secure": False},
    {"domain": "archive.md", "path": "/", "name": "HstPn2293961", "value": "1", "expires": 1807655275,
     "httpOnly": False, "secure": False},
    {"domain": "archive.md", "path": "/", "name": "HstPt2293961", "value": "1", "expires": 1807655275,
     "httpOnly": False, "secure": False},
    {"domain": "archive.md", "path": "/", "name": "HstCnv2293961", "value": "1", "expires": 1807655275,
     "httpOnly": False, "secure": False},
    {"domain": "archive.md", "path": "/", "name": "HstCns2293961", "value": "1", "expires": 1807655275,
     "httpOnly": False, "secure": False},

    # --- archive.li ---
    {"domain": ".archive.li", "path": "/", "name": "qki", "value": "4025865995672402797", "expires": 1776123053,
     "httpOnly": False, "secure": False},
    {"domain": "archive.li", "path": "/", "name": "HstCfa2293961", "value": "1776119430870", "expires": 1807655430,
     "httpOnly": False, "secure": False},
    {"domain": "archive.li", "path": "/", "name": "HstCla2293961", "value": "1776119430870", "expires": 1807655430,
     "httpOnly": False, "secure": False},
    {"domain": "archive.li", "path": "/", "name": "HstCmu2293961", "value": "1776119430870", "expires": 1807655430,
     "httpOnly": False, "secure": False},
    {"domain": "archive.li", "path": "/", "name": "HstPn2293961", "value": "1", "expires": 1807655430,
     "httpOnly": False, "secure": False},
    {"domain": "archive.li", "path": "/", "name": "HstPt2293961", "value": "1", "expires": 1807655430,
     "httpOnly": False, "secure": False},
    {"domain": "archive.li", "path": "/", "name": "HstCnv2293961", "value": "1", "expires": 1807655430,
     "httpOnly": False, "secure": False},
    {"domain": "archive.li", "path": "/", "name": "HstCns2293961", "value": "1", "expires": 1807655430,
     "httpOnly": False, "secure": False},
]


def _session_cookies(cookies: list[dict]) -> list[dict]:
    """Return cookies without expires so Chromium treats them as session cookies."""
    return [{k: v for k, v in cookie.items() if k != "expires"} for cookie in cookies]


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

    async def _prepare_context(self, context) -> None:
        cookies = _session_cookies(COOKIES)
        # Mirror archive.is cookies onto archive.today (tests/users often use that host).
        for cookie in list(cookies):
            domain = cookie.get("domain", "")
            if domain in ("archive.is", ".archive.is"):
                mirrored = dict(cookie)
                mirrored["domain"] = domain.replace("archive.is", "archive.today")
                cookies.append(mirrored)
        try:
            await context.add_cookies(cookies)
        except Exception:
            logger.warning("Could not add Archive.today cookies to browser context.", exc_info=True)

    async def _extract_content(self, page: Page) -> Optional[ContentTarget]:
        try:
            body_text = (await page.locator("body").inner_text()).lower()
        except Exception:
            logger.debug("Could not read Archive.today body text", exc_info=True)
            body_text = ""

        # Catch missing capture
        if "Not Found (yet?)".lower() in body_text.lower():
            raise TargetUnavailableError("Archive.today capture not found.")

        # Detect CAPTCHA gate
        if any(marker in body_text for marker in (
            "security check", "captcha", "just a moment", "performing security verification",
        )):
            raise RuntimeError("Archive.today asks to solve a captcha. Cannot access archived content.")

        # CAPTCHA gate often redirects to the bare host with no snapshot path.
        if urlparse(page.url).path in ("", "/"):
            raise RuntimeError("Archive.today asks to solve a captcha. Cannot access archived content.")

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
