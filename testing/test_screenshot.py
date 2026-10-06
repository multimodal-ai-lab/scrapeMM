"""Screenshots on request: taken by the method that renders the page, in the same session
as the retrieval -- never by loading the page again -- and delivered like a medium."""

import base64
import io

import aiohttp
import pytest
from PIL import Image as PillowImage

from scrapemm.common.wire import ItemDescriptor, ResponsePayload
from scrapemm.server import chain, challenges, engine, reachability
from scrapemm.server import screenshot as screenshot_module
from scrapemm.server.cache import clear_cache
from scrapemm.server.challenges import ChallengeStore
from scrapemm.server.integrations.firecrawl.firecrawl import Firecrawl
from scrapemm.server.util import to_scraped_content

pytestmark = pytest.mark.server

URL = "https://example.com/article"
PAGE = "<html><body><main><p>The article text.</p></main></body></html>"


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    PillowImage.new("RGB", (width, height), "white").save(buffer, "PNG")
    return buffer.getvalue()


class FakePage:
    """Enough of a Playwright page for a screenshot of a page `height` pixels tall."""
    viewport_size = {"width": 1920, "height": 1080}
    url = URL

    def __init__(self, height: int):
        self.height = height
        self.clip = None

    async def evaluate(self, script):
        return self.height

    async def screenshot(self, clip, **kwargs):
        self.clip = clip
        return _png(int(clip["width"]), int(clip["height"]))


async def test_capture_is_the_top_of_the_page():
    page = FakePage(height=10_000)
    png = await screenshot_module.capture(page)
    assert page.clip["height"] == 3 * 1080  # Three screens at most
    image = await screenshot_module.to_image(png, URL)
    assert max(image.width, image.height) == 2048  # Then kept at 2048 px like any image


async def test_a_long_full_page_is_cut_to_three_screens():
    image = await screenshot_module.to_image(_png(1920, 20_000), URL)  # As from Firecrawl
    assert image.height / image.width == pytest.approx(3 * 1080 / 1920, rel=0.01)


def test_firecrawl_screenshot_as_data_url():
    png = _png(4, 4)
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    assert screenshot_module.from_data_url(data_url) == png
    assert screenshot_module.from_data_url("data:text/html;base64,AAAA") is None


async def test_firecrawl_reads_its_inline_screenshot():
    png = _png(4, 4)
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    assert await Firecrawl._screenshot_bytes(data_url, session=None) == png


@pytest.fixture
def browser_serves(monkeypatch, tmp_path):
    """The browser returns the page, capturing it as the real one does when asked to."""
    async def reachable(url):
        return "ok", ""

    monkeypatch.setattr(reachability, "check", reachable)
    monkeypatch.setattr(engine.blacklist, "reason", lambda domain: None)
    monkeypatch.setattr(chain, "is_enabled", lambda key: True)
    monkeypatch.setattr(challenges, "store", ChallengeStore(path=tmp_path / "challenges.json"))
    clear_cache()
    loads = []

    def serve(html: str, renders: bool = True):
        async def get(url, output_format="multimodal", **kwargs):
            loads.append(url)
            png = _png(1920, 1080) if renders and screenshot_module.wanted() else None
            content = await to_scraped_content(html, session=None, output_format=output_format)
            screenshot_module.keep(content, png)
            return content
        monkeypatch.setattr(engine.browser, "_get", get)
        return loads

    yield serve
    clear_cache()


async def retrieve(session, screenshot: bool):
    return await engine.retrieve_one(URL, session, methods=["browser"], output_format="html",
                                     screenshot=screenshot)


async def test_screenshot_comes_from_the_retrieving_session(browser_serves):
    loads = browser_serves(PAGE)
    async with aiohttp.ClientSession() as session:
        shot = await retrieve(session, screenshot=True)
    assert shot.success and shot.screenshot is not None
    assert len(loads) == 1  # The page was loaded once, for both
    assert not hasattr(shot.content, "_screenshot")  # Taken off the content


async def test_cache_answers_screenshot_requests_only_with_one(browser_serves):
    loads = browser_serves(PAGE)
    async with aiohttp.ClientSession() as session:
        plain = await retrieve(session, screenshot=False)
        assert plain.screenshot is None and len(loads) == 1
        # The cached result has no screenshot: retrieved again, now with one
        shot = await retrieve(session, screenshot=True)
        assert shot.screenshot is not None and not shot.from_cache and len(loads) == 2
        # Which then serves both kinds of request; the plain one without it
        again = await retrieve(session, screenshot=True)
        assert again.from_cache and again.screenshot is not None
        plain_again = await retrieve(session, screenshot=False)
        assert plain_again.from_cache and plain_again.screenshot is None
    assert len(loads) == 2


async def test_no_screenshot_from_a_method_that_does_not_render(browser_serves):
    loads = browser_serves(PAGE, renders=False)  # As plain HTTP, Decodo or an API
    async with aiohttp.ClientSession() as session:
        response = await retrieve(session, screenshot=True)
    assert response.success and response.screenshot is None and len(loads) == 1


def test_screenshot_travels_over_the_wire():
    descriptor = ItemDescriptor(ref="<image:7>", kind="image", id=7, media_url="/v1/media/image/7")
    payload = ResponsePayload(url=URL, screenshot=descriptor)
    assert ResponsePayload.from_dict(payload.to_dict()).screenshot == descriptor
    assert ResponsePayload.from_dict({"url": URL}).screenshot is None
