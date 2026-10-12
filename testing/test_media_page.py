"""A page with little text waits for its content, as a shell would -- unless it is a
media page, whose content is the image or video it already shows (mvau.lt waited out
the full 15 s for text that never comes)."""

import pytest
from playwright.async_api import async_playwright

from scrapemm.server.integrations.headed_browser import DOM_THIN_TEXT, _is_thin

pytestmark = pytest.mark.server

# Pixel-free stand-ins that still have a size: the check reads naturalWidth and the box
SVG = ("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='{w}' "
       "height='{h}'><rect width='{w}' height='{h}'/></svg>")


def _image(width: int, shown: int) -> str:
    return f'<img src="{SVG.format(w=width, h=width)}" style="width:{shown}px;height:{shown}px">'


async def _thin(body: str) -> bool:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(f"<html><body>{body}</body></html>")
            await page.wait_for_timeout(200)  # The images decode
            return await _is_thin(page, DOM_THIN_TEXT)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_a_media_page_is_not_a_shell():
    caption = "<p>" + "A caption. " * 30 + "</p>"  # ~330 characters, like mvau.lt
    assert not await _thin(caption + _image(1080, 600))
    assert not await _thin(caption + _image(400, 700))  # Small file, shown large


@pytest.mark.asyncio
async def test_a_shell_waits_for_its_text_even_beside_an_image():
    caption = "<p>" + "A caption. " * 30 + "</p>"
    assert await _thin(caption + _image(200, 100))  # A logo is no medium
    shell = "<p>" + "Navigation and teaser text. " * 22 + "</p>"  # ~600 characters
    assert await _thin(shell + _image(1080, 600))  # An article's shell with its hero image
