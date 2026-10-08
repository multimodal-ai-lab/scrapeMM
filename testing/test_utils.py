import aiohttp
import pytest
from bs4 import BeautifulSoup

from scrapemm.server.download.common import HEADERS
from scrapemm.server.util import (
    _extract_media_elements,
    get_markdown_hyperlinks,
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


CHART_SVG = (b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="800" height="400">'
             b'<rect width="800" height="400" fill="#1F5A63"/></svg>')
ICON_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" stroke-width="900"/>'
HUGE_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40000 20000"/>'


def test_svg_charts_are_rasterized():
    from scrapemm.server.download.images import image_from_binary, image_size
    assert image_size(image_from_binary(CHART_SVG, "https://example.com/chart.svg")) == (800, 400)
    # Icons are not even rendered; a huge canvas is rendered at the limit straight away
    assert image_from_binary(ICON_SVG, "https://example.com/icon.svg") is None
    assert image_size(image_from_binary(HUGE_SVG, "https://example.com/huge.svg")) == (2048, 1024)


def test_svg_img_extracted_unless_shown_small():
    soup = BeautifulSoup('<img src="/chart.svg"><img src="/logo.svg" width="120">'
                         '<img src="/figure.eps">', "html.parser")
    assert [e["src"] for e in _extract_media_elements(soup)] == ["/chart.svg"]


def _data_uri(width: int, height: int) -> str:
    import base64
    from io import BytesIO
    from PIL import Image as PillowImage
    buffer = BytesIO()
    PillowImage.new("RGB", (width, height), "red").save(buffer, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def test_inline_images_are_decoded_once_and_icons_never():
    """A consent banner's vendor list repeats one inline icon a thousand times (as on
    borkenerzeitung.de): none of them may be registered, and a repeated large inline
    image only once."""
    from scrapemm.server.util import _resolve_base64_media
    icon, photo = _data_uri(18, 18), _data_uri(400, 300)
    resolved = _resolve_base64_media([(None, icon)] * 3 + [(None, photo)] * 2,
                                     source_url="https://example.com/article")
    assert resolved[:3] == [None, None, None]
    assert resolved[3] is not None and resolved[3] is resolved[4]


async def test_media_in_consent_banners_are_not_resolved(monkeypatch):
    from scrapemm.server import util
    fetched = []

    async def fake_download_image(url, *args, **kwargs):
        fetched.append(url)
        return None

    monkeypatch.setattr(util, "download_image", fake_download_image)
    html = ('<html><body><div id="CybotCookiebotDialog"><p>Wir verwenden Cookies</p>'
            '<img src="https://cdn.example.com/vendor-logo.png"></div>'
            '<article><h1>Headline</h1><img src="https://example.com/photo.jpg"></article>'
            '</body></html>')
    sequence = await util.resolve_media(html, session=None, url="https://example.com/article")
    assert fetched == ["https://example.com/photo.jpg"]
    assert "vendor-logo.png" not in str(sequence)
    assert "Wir verwenden Cookies" in str(sequence)
