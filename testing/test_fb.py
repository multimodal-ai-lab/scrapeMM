"""Facebook's login wall is no content: neither its illustration as a post's photo, nor
the wall itself as a photo page."""

import pytest
from playwright.async_api import async_playwright

from scrapemm.server.integrations.fb import (JS_GET_PHOTO_IMAGE, _is_facebook_graphic,
                                             _shows_login_wall)

pytestmark = pytest.mark.server

ILLUSTRATION = "https://static.xx.fbcdn.net/rsrc.php/yR/r/login-illustration.png"
PHOTO = "https://scontent-fra5-1.xx.fbcdn.net/v/t39.30808-6/photo_n.jpg?stp=dst-jpg"

# A pixel-free stand-in that still has a size: the script reads naturalWidth
def _img(src: str, alt: str = "photo", width: int = 1240) -> str:
    return (f'<img alt="{alt}" src="{src}" width="{width}" height="{width}" '
            f'style="width:{width}px;height:{width}px">')


async def _picked(html: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())  # Nothing leaves the test
            await page.set_content(html)
            return await page.evaluate(JS_GET_PHOTO_IMAGE)
        finally:
            await browser.close()


async def test_the_login_walls_illustration_is_never_the_photo():
    login_wall = (f'<html><head><meta property="og:image" content="{ILLUSTRATION}"></head><body>'
                  f'<form id="login_form" action="/login/"></form>{_img(ILLUSTRATION)}</body></html>')
    assert await _picked(login_wall) is None


async def test_a_photo_beside_facebooks_graphics_is_found():
    page = (f'<html><body>{_img(ILLUSTRATION)}<div role="main">{_img(PHOTO)}</div>'
            f'</body></html>')
    assert await _picked(page) == PHOTO


def test_login_wall_and_graphics_are_recognized():
    assert _is_facebook_graphic(ILLUSTRATION) and not _is_facebook_graphic(PHOTO)
    assert _shows_login_wall('<form id="login_form" action="/login/device-based/regular/login/">')
    assert _shows_login_wall("<html></html>", "https://www.facebook.com/login/?next=https%3A%2F%2Fx")
    assert not _shows_login_wall("<html><body>A post</body></html>",
                                 "https://www.facebook.com/photo/?fbid=1")
