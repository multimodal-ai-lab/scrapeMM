"""Tests for `strip`: UI elements are removed from the result on the way out, never from
what is scraped, detected on or cached."""

import aiohttp
import pytest
from ezmm import Image, MultimodalSequence
from PIL import Image as PillowImage

from scrapemm import CaptchaEncounteredError
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.common.wire import ResponsePayload
from scrapemm.server import chain, challenges, engine, reachability
from scrapemm.server.cache import cache, clear_cache
from scrapemm.server.challenges import ChallengeStore
from scrapemm.server.engine import retrieve_one
from scrapemm.server.util import _remove_ui_elements, remove_ui_elements, strip_content, to_scraped_content

pytestmark = pytest.mark.server

URL = "https://example.com/article"

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

# A challenge whose only conclusive marker sits in an overlay, which gets stripped
CAPTCHA_PAGE = ('<html><body><main><p>The article text.</p></main><div class="modal">'
                '<iframe src="https://geo.captcha-delivery.com/captcha/"></iframe>'
                '</div></body></html>')


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


# Reduced from https://www.borkenerzeitung.de/welt/in-ausland/politik-inland/Demonstranten-fordern-Ende-des-Hungers-in-Gaza-653442.html
# (Cookiebot, user-reported), plus banners of other platforms and of none
CONSENT_BANNERS = [
    '<div aria-labelledby="CybotCookiebotDialogBodyContentTitle" class="CybotMultilevel '
    'CybotCookiebotDialogActive" id="CybotCookiebotDialog" role="region">'
    '<div id="CybotCookiebotDialogHeader"><img src="https://consent.cookiebot.com/logo.png"></div>'
    '<h2 id="CybotCookiebotDialogBodyContentTitle">Verantwortungsvoller Umgang mit Ihren Daten</h2>'
    '<div id="CybotCookiebotDialogBodyContentText">Wir verwenden Cookies, um Inhalte und Anzeigen '
    'zu personalisieren. Sie können Ihre Einwilligung jederzeit ändern.</div>'
    '<button id="CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll">Zustimmen</button></div>',
    '<div id="onetrust-consent-sdk"><div id="onetrust-banner-sdk">We value your privacy. '
    '<button>Accept All Cookies</button></div></div>',
    '<div id="usercentrics-root"></div><div id="sp_message_container_1234">'
    '<iframe src="https://cmp.example.com/index.html"></iframe></div>',
    '<div class="site-consent-layer"><p>Nous utilisons des traceurs. Votre consentement ?</p>'
    '<button>Tout accepter</button></div>',
    # Tarteaucitron (science.feedback.org)
    '<div id="tarteaucitronRoot"><div id="tarteaucitronAlertBig">Cookies management panel '
    '<button>Allow all cookies</button></div></div>',
    # Google Funding Choices (boatos.org), with a push-notification prompt beside it
    '<div class="fc-consent-root"><div class="fc-dialog-container">boatos.org solicita o seu '
    'consentimento. <button>Consentir</button></div></div>'
    '<div id="perfecty-push-dialog-container" class="site perfecty-push-dialog-container">'
    'Se inscreva para receber nossas atualizações <button>Permitir</button></div>',
]


@pytest.mark.parametrize("banner", CONSENT_BANNERS)
def test_remove_ui_elements_removes_consent_banners(banner: str):
    page = (f'<html><body>{banner}<div id="page"><h1>Gewalt bei Palästina-Demo</h1>'
            '<p>Police break up a pro-Palestinian demonstration at Checkpoint Charlie.</p>'
            '<img src="https://example.com/photo.jpg"></div></body></html>')
    html, removed_media = _remove_ui_elements(page)
    for consent in ("Cookies", "Einwilligung", "Zustimmen", "privacy", "consentement",
                    "cmp.example.com", "cookiebot.com"):
        assert consent not in html
    for content in ("Gewalt bei Palästina-Demo", "Checkpoint Charlie", "photo.jpg"):
        assert content in html
    assert "https://example.com/photo.jpg" not in removed_media


def test_remove_ui_elements_keeps_content_about_cookies():
    """Neither a page wrapper that records the consent in its class nor an article
    on cookies is a banner."""
    page = ('<html><body><div class="cookies-accepted"><h1>Cookie law</h1>'
            '<p>The new consent rules for cookies explained.</p></div></body></html>')
    assert "The new consent rules for cookies explained." in remove_ui_elements(page)


def test_remove_ui_elements_keeps_page_it_would_empty():
    html = '<html><head><title>Site</title></head><body class="side"><p>All there is</p></body></html>'
    assert remove_ui_elements(html) == html


async def test_strip_content_leaves_original_untouched():
    original = await to_scraped_content(PAGE_WITH_UI, session=None, output_format="markdown")
    stripped = await strip_content(original, url=URL)
    assert stripped.stripped and not original.stripped
    assert "Home" not in stripped.html and "Home" not in stripped.markdown
    assert "The article text." in stripped.markdown
    assert original.html == PAGE_WITH_UI and "Home" in original.markdown
    assert await strip_content(stripped) is stripped  # Not twice


async def test_strip_content_without_html_is_unchanged():
    content = ScrapedContent(markdown="A post from an API integration")
    assert await strip_content(content) is content


def _image(url: str, color: str = "red") -> Image:
    return Image(pillow_image=PillowImage.new("RGB", (300, 300), color), source_url=url)


async def test_strip_content_reuses_media_without_fetching():
    """The stripped sequence takes its media from the original one; a session of None
    would fail on any download."""
    logo = _image("https://example.com/logo.png", "navy")
    photo = _image("https://example.com/photo.jpg", "teal")
    html = ('<html><body><header><img src="/logo.png"></header>'
            '<main><p>Text</p><img src="photo.jpg"></main></body></html>')
    original = ScrapedContent(html=html, markdown="Text",
                              multimodal=MultimodalSequence(f"{logo.reference} Text {photo.reference}"))
    stripped = await strip_content(original, url=URL)
    assert photo.reference in str(stripped.multimodal)
    assert logo.reference not in str(stripped.multimodal)
    assert "Text" in str(stripped.multimodal)



async def test_strip_content_finds_a_picture_under_its_second_address():
    """ezMM merges identical files into one item: the photo, retrieved again under
    another address, is that same item, found by either address."""
    first = _image("https://cdn.example.com/a/photo.jpg", "olive")
    again = _image("https://example.com/photo.jpg", "olive")
    assert again.reference == first.reference  # One item
    html = '<html><body><nav>Menu</nav><main><p>Text</p><img src="photo.jpg"></main></body></html>'
    original = ScrapedContent(html=html, multimodal=MultimodalSequence(f"Text {again.reference}"))
    stripped = await strip_content(original, url=URL)
    assert first.reference in str(stripped.multimodal)
    assert "Menu" not in str(stripped.multimodal)

async def test_strip_content_keeps_media_it_cannot_place():
    """A medium not traceable to an element (here: a video the browser collected from
    the page beyond its HTML) is no UI element as far as anybody can tell."""
    video = _image(URL)  # Its source is the page itself
    html = '<html><body><nav>Menu</nav><main><p>Text</p></main></body></html>'
    original = ScrapedContent(html=html, multimodal=MultimodalSequence(f"Text {video.reference}"))
    stripped = await strip_content(original, url=URL)
    assert video.reference in str(stripped.multimodal)
    assert "Menu" not in str(stripped.multimodal)


async def test_strip_content_does_not_hold_the_loop_while_the_registry_is_busy():
    """Media downloads register their files in threads, holding the registry's lock
    meanwhile. Looking up the known media must wait for it off the loop, or every
    request stalls with it."""
    import asyncio
    import threading
    import time
    from ezmm.common.registry import item_registry

    photo = _image("https://example.com/photo.jpg", "maroon")
    html = '<html><body><nav>Menu</nav><main><p>Text</p><img src="photo.jpg"></main></body></html>'
    original = ScrapedContent(html=html, multimodal=MultimodalSequence(f"Text {photo.reference}"))

    held, release = threading.Event(), threading.Event()

    def hold_lock():
        with item_registry._lock:
            held.set()
            release.wait(5)

    threading.Thread(target=hold_lock, daemon=True).start()
    held.wait(5)
    stripping = asyncio.create_task(strip_content(original, url=URL))
    started = time.monotonic()
    await asyncio.sleep(0.3)  # Returns late if the loop is held
    lag = time.monotonic() - started - 0.3
    release.set()
    stripped = await stripping
    assert lag < 0.2
    assert photo.reference in str(stripped.multimodal)


@pytest.fixture
def browser_serves(monkeypatch, tmp_path):
    """Makes the browser return the given HTML, with the host reachable and not
    blacklisted, the browser method enabled, and the CAPTCHA queue in a throwaway file.
    Counts the browser's calls."""
    async def reachable(url):
        return "ok", ""

    monkeypatch.setattr(reachability, "check", reachable)
    monkeypatch.setattr(engine.blacklist, "reason", lambda domain: None)
    monkeypatch.setattr(chain, "is_enabled", lambda key: True)
    monkeypatch.setattr(challenges, "store", ChallengeStore(path=tmp_path / "challenges.json"))
    clear_cache()
    calls = []

    def serve(html: str):
        async def get(url, output_format="multimodal", **kwargs):
            calls.append(url)
            return await to_scraped_content(html, session=None, output_format=output_format)
        monkeypatch.setattr(engine.browser, "_get", get)
        return calls

    yield serve
    clear_cache()


async def test_stripped_and_whole_share_one_scrape(browser_serves):
    calls = browser_serves(PAGE_WITH_UI)
    async with aiohttp.ClientSession() as session:
        stripped = await retrieve_one(URL, session, methods=["browser"], output_format="markdown",
                                      strip=True)
        whole = await retrieve_one(URL, session, methods=["browser"], output_format="markdown")
    assert stripped.success and whole.success, (stripped.errors, whole.errors)
    assert stripped.content.stripped and "Home" not in stripped.content.html
    assert not whole.content.stripped and whole.content.html == PAGE_WITH_UI
    assert whole.from_cache
    assert len(calls) == 1
    assert len(cache) == 1


async def test_captcha_detection_sees_the_whole_page(browser_serves):
    """Stripped first, the challenge would lose its marker and pass for content."""
    browser_serves(CAPTCHA_PAGE)
    async with aiohttp.ClientSession() as session:
        response = await retrieve_one(URL, session, methods=["browser"], output_format="html",
                                      strip=True)
    assert not response.success
    assert isinstance(response.errors["browser"], CaptchaEncounteredError)


def test_stripped_travels_over_the_wire():
    payload = ResponsePayload.from_dict({"url": URL, "output_format": "html",
                                         "content": {"html": "<p>Hi</p>", "stripped": True}})
    assert payload.to_response().content.stripped
    assert ResponsePayload.from_dict(payload.to_dict()).content.stripped
