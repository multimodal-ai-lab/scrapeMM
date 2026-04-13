"""Integration for Archive.today retrieval."""

import logging
from io import BytesIO
from urllib.parse import urljoin

import PIL.Image
from ezmm import MultimodalSequence, Image, Video, Item
from markdownify import markdownify as md
from playwright.async_api import TimeoutError, async_playwright, BrowserContext, Page
from playwright_stealth import Stealth

from scrapemm.download.common import HEADERS
from scrapemm.integrations.base import RetrievalIntegration
from scrapemm.util import postprocess_scraped, is_data_uri, decompose_data_uri, from_base64, \
    get_markdown_hyperlinks

logger = logging.getLogger("scrapeMM")

ARCHIVE_TODAY_CONTENT_DIV_ID = "CONTENT"

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff")
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".flv", ".wmv", ".ts")

cookies = [
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


# ---------------------------------------------------------------------------
# Media helpers
# ---------------------------------------------------------------------------

async def _download_media_with_playwright(api_request_context, src: str, base_url: str) -> bytes | None:
    """Download media bytes using Playwright's API request context (same session/cookies)."""
    url = src if src.startswith("http") else urljoin(base_url, src)
    try:
        response = await api_request_context.get(url, headers={"Referer": base_url})
        if response.ok:
            return await response.body()
    except Exception as e:
        logger.debug(f"Failed to download media from {url}: {type(e).__name__}: {e}")
    return None


def _bytes_to_image(data: bytes, source_url: str, max_size: tuple[int, int] = (2048, 2048)) -> Image | None:
    """Convert raw bytes to an ezmm Image, skipping small or unidentifiable images."""
    try:
        pillow_img = PIL.Image.open(BytesIO(data))
    except (PIL.UnidentifiedImageError, Exception):
        return None

    if pillow_img.width <= 256 or pillow_img.height <= 256:
        return None

    if pillow_img.width > max_size[0] or pillow_img.height > max_size[1]:
        pillow_img.thumbnail(max_size, PIL.Image.Resampling.LANCZOS)

    image = Image(pillow_image=pillow_img, source_url=source_url)
    image.relocate(move_not_copy=True)
    return image


def _bytes_to_video(data: bytes, source_url: str) -> Video | None:
    """Convert raw bytes to an ezmm Video."""
    try:
        video = Video(binary_data=data, source_url=source_url)
        video.relocate(move_not_copy=True)
        return video
    except Exception:
        return None


def _resolve_media_from_base64(data_uri: str, page_url: str) -> Item | None:
    """Decode a base64 data URI into an Image or Video, excluding small images."""
    result = decompose_data_uri(data_uri)
    if result is None:
        return None
    mime_type, b64_data = result
    item = from_base64(b64_data, mime_type=mime_type, url=page_url)
    if isinstance(item, Image) and (item.width <= 256 or item.height <= 256):
        return None
    return item


# ---------------------------------------------------------------------------
# Page-level extraction helpers
# ---------------------------------------------------------------------------

async def _collect_media_srcs(content_el) -> tuple[list[str], list[str]]:
    """Extract image and video src attributes from the content element."""
    img_elements = content_el.locator("img")
    img_srcs = []
    for i in range(await img_elements.count()):
        src = await img_elements.nth(i).get_attribute("src")
        if src:
            img_srcs.append(src)

    video_srcs = []
    video_elements = content_el.locator("video[src]")
    for i in range(await video_elements.count()):
        src = await video_elements.nth(i).get_attribute("src")
        if src:
            video_srcs.append(src)
    source_elements = content_el.locator("video source[src]")
    for i in range(await source_elements.count()):
        src = await source_elements.nth(i).get_attribute("src")
        if src:
            video_srcs.append(src)

    return img_srcs, video_srcs


async def _download_all_media(
        api_request_context,
        img_srcs: list[str],
        video_srcs: list[str],
        page_url: str,
) -> dict[str, Item | None]:
    """Download (or decode) all collected media sources and return a src→Item mapping."""
    media: dict[str, Item | None] = {}

    for src in img_srcs:
        if src in media:
            continue
        if is_data_uri(src):
            media[src] = _resolve_media_from_base64(src, page_url)
        elif src.startswith("data:"):
            # Non-base64 data URIs (e.g. URL-encoded SVGs) – skip
            continue
        else:
            data = await _download_media_with_playwright(api_request_context, src, page_url)
            full_url = src if src.startswith("http") else urljoin(page_url, src)
            media[src] = _bytes_to_image(data, full_url) if data else None

    for src in video_srcs:
        if src in media:
            continue
        if is_data_uri(src):
            media[src] = _resolve_media_from_base64(src, page_url)
        elif src.startswith("data:"):
            continue
        else:
            data = await _download_media_with_playwright(api_request_context, src, page_url)
            full_url = src if src.startswith("http") else urljoin(page_url, src)
            media[src] = _bytes_to_video(data, full_url) if data else None

    return media


def _replace_media_in_markdown(markdown_text: str, media: dict[str, Item | None]) -> str:
    """Replace markdown hyperlinks whose href matches a downloaded medium with inline references."""
    for full_match, hypertext, href in get_markdown_hyperlinks(markdown_text):
        medium = media.get(href)
        if medium is not None:
            replacement = f"{hypertext} {medium.reference}" if hypertext else medium.reference
            markdown_text = markdown_text.replace(full_match, replacement)
    return markdown_text


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


# ---------------------------------------------------------------------------
# Integration class
# ---------------------------------------------------------------------------

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

    async def _get(self, url: str, **kwargs) -> MultimodalSequence | None:
        return await self.download_record(url)

    async def download_record(self, url: str) -> MultimodalSequence | None:
        """
        Retrieves the archived web page content as a MultimodalSequence.
        Uses a single Playwright session for both page loading and media downloading,
        avoiding session mismatch issues.

        Archive.today occasionally requires solving captchas. To bypass these,
        we use Playwright with stealth settings (provided by playwright-stealth)
        and a custom user-agent with real cookies.

        Args:
            url (str): The URL of the archived page on Archive.today.
        Returns:
            MultimodalSequence | None: The multimodal content, or None if retrieval fails.
        """
        # Download content and media
        async with Stealth().use_async(async_playwright()) as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=HEADERS["User-Agent"])
            await context.add_cookies(cookies)

            try:
                page = await _load_page(context, url)
                if page is None:
                    return None

                content_html = await _extract_content_html(page)
                if content_html is None:
                    return None

                content_el = page.locator(f"#{ARCHIVE_TODAY_CONTENT_DIV_ID}")
                img_srcs, video_srcs = await _collect_media_srcs(content_el)
                media = await _download_all_media(context.request, img_srcs, video_srcs, url)
            finally:
                await browser.close()

        # Convert HTML to Markdown
        try:
            markdown_text = md(content_html, heading_style="ATX")
        except RecursionError:
            return None
        markdown_text = postprocess_scraped(markdown_text)

        # Inline downloaded media into the Markdown text
        markdown_text = _replace_media_in_markdown(markdown_text, media)

        return MultimodalSequence(markdown_text)
