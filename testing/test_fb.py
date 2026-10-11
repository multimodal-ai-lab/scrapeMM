"""Facebook's login wall is no content: neither its illustration as a post's photo, nor
the wall itself as a photo page. Behind the wall, a post's embed is read instead."""

import pytest
from playwright.async_api import async_playwright

from scrapemm.common import RetrievalFailed
from scrapemm.common.exceptions import TargetUnavailableError
from scrapemm.server.integrations.fb import (JS_GET_PHOTO_IMAGE, Facebook,
                                             _is_facebook_graphic, _parse_embed,
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


# Facebook's embeds (plugins/post.php), cut down to what is read off them: a post with
# photos, laid out as a post's embed, and a single photo's, laid out differently
AVATAR = "https://scontent.xx.fbcdn.net/v/t39.30808-1/avatar_n.jpg"
ICON = "https://static.xx.fbcdn.net/rsrc.php/yW/r/1bTHYerGQAq.webp"
POST_EMBED = f"""
<div class="_5pcr userContentWrapper">
  <a class="_5pb8"><img class="_s0 _4ooo img" src="{AVATAR}"></a>
  <div class="_6a"><div class="_2_79 _50f4 _50f7">Some Page</div></div>
  <a href="/TheWolverLing/posts/1225874212228936?ref=embed_post"><abbr data-utime="1743515870">about 2 years ago</abbr></a>
  <div class="_5pbx userContent _3576"><p>First line</p><p>Second line</p>
    <a class="see_more_link">See more</a></div>
  <img class="img" src="{ICON}" width="24" height="24">
  <div class="uiScaledImageContainer"><img class="scaledImageFitWidth img" src="{PHOTO}&amp;a=1"></div>
  <div class="uiScaledImageContainer"><img class="scaledImageFitWidth img" src="{PHOTO}&amp;a=2"></div>
</div>"""
PHOTO_EMBED = f"""
<div class="_2iem"><a><span class="_2_79 _50f7">Some Person</span></a></div>
<img class="_s0 _4ooo img" src="{AVATAR}">
<a href="https://www.facebook.com/photo.php?fbid=1721085455188778&amp;ref=embed_post"></a>
<abbr data-utime="1750289122">about a year ago</abbr>
<div class="_5pbx userContent _3576"><p>Breaking: a caption</p></div>
<a href="https://www.facebook.com/bhim.upadhyaya.180/posts/1721085478522109?ref=embed_post">a year ago</a>
<img class="_1p6f _1p6g img" src="{PHOTO}" width="497" height="498">"""
GONE_EMBED = """<div><img class="_1q3x img" src="https://static.xx.fbcdn.net/rsrc.php/ya/r/x.webp">
This Facebook post is no longer available. It may have been removed or the privacy
settings of the post may have changed.</div>"""


def test_a_posts_embed_yields_its_text_and_photos_but_no_avatar_or_icon():
    header, photos, post_url = _parse_embed(POST_EMBED)
    assert photos == [f"{PHOTO}&a=1", f"{PHOTO}&a=2"]
    assert post_url == "https://www.facebook.com/TheWolverLing/posts/1225874212228936"
    assert "Author: Some Page" in header and "Posted: 2025-04-01 13:57 UTC" in header
    assert "First line" in header and "Second line" in header
    assert header.endswith("…")  # Cut short by the embed


def test_a_photos_embed_yields_the_photo_and_its_caption():
    header, photos, post_url = _parse_embed(PHOTO_EMBED)
    assert photos == [PHOTO]
    # The post the photo belongs to, whose page has the caption in full
    assert post_url == "https://www.facebook.com/bhim.upadhyaya.180/posts/1721085478522109"
    assert "Author: Some Person" in header and "Breaking: a caption" in header
    assert not header.endswith("…")


def test_an_embed_of_a_removed_post_is_unavailable_and_an_empty_one_a_failure():
    with pytest.raises(TargetUnavailableError):
        _parse_embed(GONE_EMBED)
    with pytest.raises(RetrievalFailed):
        _parse_embed("<html><body>Something else entirely</body></html>")


def test_a_permalink_becomes_the_posts_page_the_server_may_see():
    """permalink.php redirects the server to the login page; /<owner>/posts/<id> does not."""
    normalize = Facebook.__new__(Facebook)._normalize_url
    assert normalize("https://www.facebook.com/permalink.php?story_fbid=pfbid02CP&#038;id=100078635210746") \
        == "https://www.facebook.com/100078635210746/posts/pfbid02CP"
    assert normalize("https://m.facebook.com/permalink.php?id=7&story_fbid=123") \
        == "https://www.facebook.com/7/posts/123"
    # Without both parts, it stays as it is
    assert normalize("https://www.facebook.com/permalink.php?story_fbid=123") \
        == "https://www.facebook.com/permalink.php?story_fbid=123"
