"""Integration for Archive.today retrieval."""

import logging
from typing import Optional

from ezmm import MultimodalSequence
from playwright.async_api import TimeoutError, async_playwright, BrowserContext, Page
from playwright_stealth import Stealth

from scrapemm.download.common import HEADERS
from scrapemm.integrations.base import RetrievalIntegration

logger = logging.getLogger("scrapeMM")

ARCHIVE_TODAY_CONTENT_DIV_ID = "CONTENT"

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


async def _load_page(context: BrowserContext, url: str) -> Page | None:
    """Navigate to *url* inside *context*, returning the Page or None on failure."""
    page = await context.new_page()
    try:
        await page.goto(url, timeout=30000)
        await page.wait_for_load_state("domcontentloaded")
    except TimeoutError as e:
        logger.warning(f"\rUnable to load page at URL '{url}'.\n\tReason: {type(e).__name__} {e}")
        return None

    body_text = (await page.locator("body").inner_text()).lower()
    if "security check" in body_text or "captcha" in body_text:
        raise RuntimeError("Archive.today asks to solve a captcha. Cannot access archived content.")

    return page


async def _extract_content_html(page: Page) -> str | None:
    """Wait for the content div and return its inner HTML, or None on timeout."""
    try:
        await page.wait_for_selector(f"#{ARCHIVE_TODAY_CONTENT_DIV_ID}", timeout=5000)
        return await page.inner_html(f"#{ARCHIVE_TODAY_CONTENT_DIV_ID}")
    except TimeoutError:
        logger.debug(f"Retrieval of archived content from '{page.url}' timed out.")
        return None


class ArchiveToday(RetrievalIntegration):
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

    async def _connect(self):
        self.connected = True

    async def _get(self, url: str, **kwargs) -> Optional[MultimodalSequence]:
        """Retrieves the archived web page content as a MultimodalSequence.

        Archive.today has strong anti-bot protection and requests captchas unless
        the recorded session cookies are present. A headless Playwright browser with
        stealth patches plus those cookies is enough to bypass the captcha, so there
        is no performance reason to spin up a full headed browser here.

        Media is resolved with the shared pipeline (``to_multimodal_sequence`` with
        ``source_element=page``): media bytes are fetched in-page via the browser's
        authenticated session, so the archive's cookies are reused automatically.
        """
        async with Stealth().use_async(async_playwright()) as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=HEADERS["User-Agent"])
            await context.add_cookies(COOKIES)

            try:
                page = await _load_page(context, url)
                if page is None:
                    return None

                content_html = await _extract_content_html(page)
                if content_html is None:
                    return None

                from scrapemm.util import to_multimodal_sequence
                return await to_multimodal_sequence(
                    content_html, session=context.request, url=url, source_element=page
                )
            finally:
                await browser.close()
