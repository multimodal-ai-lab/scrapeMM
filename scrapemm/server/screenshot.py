"""Screenshots of retrieved pages, taken on request (`retrieve(..., screenshot=True)`).

Taken by the method that renders the page, in the same session as the retrieval --
never by loading the page a second time:

* the server's browser (and the archives retrieved in it: Perma.cc, the Wayback Machine,
  Ghostarchive) captures its open page before closing it, see `capture()`;
* Firecrawl returns one along with its scrape.

Methods that do not render the page (plain HTTP, Decodo, API integrations, Archive.today's
own retrieval) yield no screenshot.

The engine asks for one per retrieval with `requested()`; the methods check `wanted()`.
What a method captured travels with its content (`keep()`) until the engine takes it
off for the response (`take()`), so it is never part of `ScrapedContent`'s interface.

What is captured is the top of the page, up to three screens high, at desktop width:
the full page of an endless news site would only be shrunk to an unreadable strip
(images are kept at 2048 px at most).
"""

import base64
import io
import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Optional

from scrapemm.common.paths import APP_NAME

if TYPE_CHECKING:
    from ezmm import Image
    from playwright.async_api import Page
    from scrapemm.common.scraping_response import ScrapedContent

logger = logging.getLogger(APP_NAME)

WIDTH, HEIGHT = 1920, 1080
MAX_SCREENS = 3

# Set per retrieval: each URL is retrieved in a task of its own, which the methods' tasks
# inherit it from
_wanted: ContextVar[bool] = ContextVar("scrapemm_screenshot", default=False)


def requested(wanted: bool) -> None:
    """Whether the retrieval running in this task wants a screenshot."""
    _wanted.set(wanted)


def wanted() -> bool:
    return _wanted.get()


async def capture(page: "Page") -> Optional[bytes]:
    """The open, loaded page as a PNG: its top, up to `MAX_SCREENS` screens high, at the
    page's width. None if that failed -- a missing screenshot never fails a retrieval."""
    try:
        size = page.viewport_size or {"width": WIDTH, "height": HEIGHT}
        height = await page.evaluate("document.documentElement.scrollHeight") or size["height"]
        return await page.screenshot(type="png", full_page=True, clip={
            "x": 0, "y": 0, "width": size["width"],
            "height": min(int(height), size["height"] * MAX_SCREENS)})
    except Exception as e:
        logger.info(f"📷 No screenshot of {getattr(page, 'url', 'the page')}: "
                    f"{type(e).__name__}: {e}")
        return None


def keep(content: Optional["ScrapedContent"], png: Optional[bytes]) -> None:
    """Hands what a method captured to the engine, along with the method's content."""
    if content is not None and png:
        content._screenshot = png


def take(content: Optional["ScrapedContent"]) -> Optional[bytes]:
    """Takes the screenshot a method kept with its content off it, if there is one."""
    return content.__dict__.pop("_screenshot", None) if content is not None else None


def from_data_url(data_url: str) -> Optional[bytes]:
    """The image of a `data:image/...;base64,` URL, as Firecrawl returns one self-hosted."""
    header, _, encoded = data_url.partition(",")
    if not header.startswith("data:image/") or ";base64" not in header:
        return None
    try:
        return base64.b64decode(encoded)
    except ValueError:
        return None


async def to_image(png: bytes, url: str) -> Optional["Image"]:
    """The screenshot as an ezMM Image. Cut to `MAX_SCREENS` screens of its width's
    proportions first: a full page from Firecrawl may be arbitrarily long."""
    from PIL import Image as PillowImage
    from .download.images import decode_image
    try:
        with PillowImage.open(io.BytesIO(png)) as picture:
            max_height = int(picture.width * HEIGHT * MAX_SCREENS / WIDTH)
            if picture.height > max_height:
                buffer = io.BytesIO()
                picture.crop((0, 0, picture.width, max_height)).save(buffer, "PNG")
                png = buffer.getvalue()
    except Exception:
        logger.debug(f"Could not read the screenshot of {url}.", exc_info=True)
        return None
    return await decode_image(png, url, ignore_small_images=False)
