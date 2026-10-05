"""Tests that `only_main_content` strips the returned HTML, but only once CAPTCHA and
paywall detection have seen the whole page. A stand-in replaces the browser."""

import pytest

from scrapemm import CaptchaEncounteredError
from scrapemm.server import chain, challenges, engine, reachability
from scrapemm.server.cache import clear_cache
from scrapemm.server.challenges import ChallengeStore
from scrapemm.server.engine import _retrieve_single
from scrapemm.server.util import to_scraped_content

pytestmark = pytest.mark.server

URL = "https://example.com/article"
PAGE = "<html><body><nav>Home</nav><main><p>The article text.</p></main></body></html>"
# A challenge whose only conclusive marker sits in an overlay, which gets stripped
CAPTCHA_PAGE = ('<html><body><main><p>The article text.</p></main><div class="modal">'
                '<iframe src="https://geo.captcha-delivery.com/captcha/"></iframe>'
                '</div></body></html>')
# A short article, with a CAPTCHA widget in a newsletter form in its footer
ARTICLE_WITH_WIDGET = ("<html><body><nav>" + "<a href='/s'>Section</a> " * 150 + "</nav>"
                       "<main><p>A short news item.</p></main><footer><form>"
                       '<div class="g-recaptcha"></div></form></footer></body></html>')


@pytest.fixture
def browser_serves(monkeypatch, tmp_path):
    """Makes the browser return the given HTML, with the host reachable and not
    blacklisted, the browser method enabled, and the CAPTCHA queue in a throwaway file."""
    async def reachable(url):
        return "ok", ""

    monkeypatch.setattr(reachability, "check", reachable)
    monkeypatch.setattr(engine.blacklist, "reason", lambda domain: None)
    monkeypatch.setattr(chain, "is_enabled", lambda key: True)
    monkeypatch.setattr(challenges, "store", ChallengeStore(path=tmp_path / "challenges.json"))
    clear_cache()

    def serve(html: str):
        async def get(url, output_format="multimodal", only_main_content=False, **kwargs):
            return await to_scraped_content(html, session=None, output_format=output_format,
                                            only_main_content=only_main_content)
        monkeypatch.setattr(engine.browser, "_get", get)

    yield serve
    clear_cache()


async def test_html_is_stripped(browser_serves):
    browser_serves(PAGE)
    response = await _retrieve_single(URL, None, methods=["browser"], output_format="html",
                                      only_main_content=True)
    assert response.success, response.errors
    assert "Home" not in response.content.html
    assert "The article text." in response.content.html


async def test_html_is_whole_by_default(browser_serves):
    browser_serves(PAGE)
    response = await _retrieve_single(URL, None, methods=["browser"], output_format="html")
    assert response.success, response.errors
    assert response.content.html == PAGE


async def test_captcha_detection_sees_the_whole_page(browser_serves):
    """Stripped first, the challenge would lose its marker and pass for content."""
    browser_serves(CAPTCHA_PAGE)
    response = await _retrieve_single(URL, None, methods=["browser"], output_format="html",
                                      only_main_content=True)
    assert not response.success
    assert isinstance(response.errors["browser"], CaptchaEncounteredError)


async def test_short_article_is_no_captcha(browser_serves):
    """Judged by its stripped length, the article would pass for a challenge."""
    browser_serves(ARTICLE_WITH_WIDGET)
    response = await _retrieve_single(URL, None, methods=["browser"], output_format="markdown",
                                      only_main_content=True)
    assert response.success, response.errors
    assert response.content.markdown == "A short news item."
