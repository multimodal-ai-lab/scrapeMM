import aiohttp
import pytest
from bs4 import BeautifulSoup

from scrapemm.server.download.common import HEADERS
from scrapemm.server.util import (
    _extract_media_elements,
    get_markdown_hyperlinks,
    remove_ui_elements,
    to_scraped_content,
    unshorten,
)

pytestmark = pytest.mark.server


@pytest.mark.parametrize("input,target",
                         [
                             (
                                     '![](https://factly.in/wp-content/uploads//2023/12/Bombay-high-court-building-featured-image-103x65.jpeg "Review: Bombay High Court Rules That Human Need for an Organ Transplant is Directly a Facet of Right to Life as Guaranteed Under Article 21 of the Constitution")',
                                     [
                                         "https://factly.in/wp-content/uploads//2023/12/Bombay-high-court-building-featured-image-103x65.jpeg"
                                     ]
                             )
                         ]
                         )
def test_media_link_extraction(input, target):
    match_hypertext_url_triples = get_markdown_hyperlinks(input)
    urls = [triple[2] for triple in match_hypertext_url_triples]
    assert urls == target


def test_extract_background_image_from_photo_wrap():
    html = (
        '<a class="tgme_widget_message_photo_wrap" '
        'style="width:641px;background-image:url(\'https://cdn4.telegram-cdn.org/file/abc123.jpg\')"></a>'
    )
    soup = BeautifulSoup(html, "html.parser")
    elements = _extract_media_elements(soup)
    assert len(elements) == 1
    assert elements[0].get("src") == "https://cdn4.telegram-cdn.org/file/abc123.jpg"


def test_extract_skips_emoji_background_image():
    html = (
        '<i class="emoji" '
        'style="background-image:url(\'//telegram.org/img/emoji/40/F09FA681.png\')"></i>'
    )
    soup = BeautifulSoup(html, "html.parser")
    assert _extract_media_elements(soup) == []


PAGE_WITH_UI = """<html><body>
<header><nav><a href="/">Home</a> <a href="/news">News</a></nav></header>
<div class="cookie">We use cookies. Accept?</div>
<main>
  <article>
    <header><h1>Headline</h1><p>By A. Author</p></header>
    <p>The article text.</p>
    <img src="https://example.com/photo.jpg">
  </article>
  <aside><article><a href="/other">A related story</a></article></aside>
</main>
<footer>Imprint and privacy</footer>
</body></html>"""


def test_remove_ui_elements():
    html = remove_ui_elements(PAGE_WITH_UI)
    for ui in ("Home", "cookies", "A related story", "Imprint"):
        assert ui not in html
    # The article's own header carries its headline and byline
    for content in ("Headline", "By A. Author", "The article text.", "photo.jpg"):
        assert content in html


@pytest.mark.parametrize("main", [
    '<div class="side"><div id="main"><p>Content</p></div></div>',
    '<div class="side"><main><p>Content</p></main></div>',
    '<div class="widget"><div role="main"><p>Content</p></div></div>',
    '<main class="top"><p>Content</p></main>',  # Matches a UI selector itself
])
def test_remove_ui_elements_keeps_main_content(main: str):
    html = remove_ui_elements(f"<html><body><nav>Menu</nav>{main}</body></html>")
    assert "Menu" not in html
    assert "Content" in html


def test_remove_ui_elements_keeps_page_it_would_empty():
    html = '<html><head><title>Site</title></head><body class="side"><p>All there is</p></body></html>'
    assert remove_ui_elements(html) == html


@pytest.mark.asyncio
async def test_to_scraped_content_removes_ui_from_markdown_only():
    content = await to_scraped_content(PAGE_WITH_UI, session=None, output_format="markdown",
                                       only_main_content=True)
    assert "Home" not in content.markdown
    assert "The article text." in content.markdown
    # CAPTCHA and paywall detection need the page as scraped
    assert content.html == PAGE_WITH_UI


async def do_unshorten(short_url):
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        return await unshorten(short_url, session)


@pytest.mark.asyncio
@pytest.mark.parametrize("short_url,long_url", [
    ("https://t.co/GcXDN4zbRx", "https://twitter.com/mossos/status/1415935363567263744/photo/1"),
])
async def test_unshorten(short_url: str, long_url: str):
    extended = await do_unshorten(short_url)
    assert extended == long_url
