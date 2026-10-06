"""Screenshots on request: taken in the server's browser after a successful retrieval,
cached by URL, delivered like a medium."""

import io

import aiohttp
import pytest
from PIL import Image as PillowImage

from scrapemm.common.wire import ItemDescriptor, ResponsePayload
from scrapemm.server import chain, challenges, engine, reachability
from scrapemm.server import screenshot as screenshot_module
from scrapemm.server.cache import clear_cache
from scrapemm.server.challenges import ChallengeStore
from scrapemm.server.util import to_scraped_content

pytestmark = pytest.mark.server

URL = "https://example.com/article"


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    PillowImage.new("RGB", (width, height), "white").save(buffer, "PNG")
    return buffer.getvalue()


class FakePage:
    """Enough of a Playwright page for a screenshot, of a page `height` pixels tall."""

    def __init__(self, height: int):
        self.height = height
        self.clip = None

    async def set_viewport_size(self, size):
        pass

    async def goto(self, url, **kwargs):
        pass

    async def wait_for_load_state(self, state, **kwargs):
        pass

    async def evaluate(self, script):
        return self.height

    async def screenshot(self, clip, **kwargs):
        self.clip = clip
        return _png(int(clip["width"]), int(clip["height"]))


@pytest.fixture
def fake_browser(monkeypatch):
    from scrapemm.server.integrations import browser, headed_browser
    pages = []

    async def new_page(p=None):
        pages.append(FakePage(height=10_000))
        return pages[-1], 0

    async def release(page):
        pass

    monkeypatch.setattr(browser, "_new_page", new_page)
    monkeypatch.setattr(headed_browser, "release_page", release)
    monkeypatch.setattr(screenshot_module, "_cached", type(screenshot_module._cached)())
    return pages


async def test_screenshot_is_the_top_of_the_page(fake_browser):
    image = await screenshot_module.screenshot(URL)
    # Three screens at most, then shrunk to the 2048 px all images are kept at
    assert fake_browser[0].clip["height"] == 3 * 1080
    assert max(image.width, image.height) == 2048
    # Cached: asked again, no page is loaded
    assert await screenshot_module.screenshot(URL) is image
    assert len(fake_browser) == 1
    await screenshot_module.screenshot(URL, use_cache=False)
    assert len(fake_browser) == 2


@pytest.fixture
def browser_serves(monkeypatch, tmp_path):
    async def reachable(url):
        return "ok", ""

    monkeypatch.setattr(reachability, "check", reachable)
    monkeypatch.setattr(engine.blacklist, "reason", lambda domain: None)
    monkeypatch.setattr(chain, "is_enabled", lambda key: True)
    monkeypatch.setattr(challenges, "store", ChallengeStore(path=tmp_path / "challenges.json"))
    clear_cache()
    taken = []

    async def fake_screenshot(url, use_cache=True):
        taken.append(url)
        return "a screenshot"

    monkeypatch.setattr(screenshot_module, "screenshot", fake_screenshot)

    def serve(html):
        async def get(url, output_format="multimodal", **kwargs):
            return await to_scraped_content(html, session=None, output_format=output_format)
        monkeypatch.setattr(engine.browser, "_get", get)
        return taken

    yield serve
    clear_cache()


async def test_screenshot_only_when_asked_and_retrieved(browser_serves):
    taken = browser_serves("<html><body><main><p>The article text.</p></main></body></html>")
    async with aiohttp.ClientSession() as session:
        plain = await engine.retrieve_one(URL, session, methods=["browser"], output_format="html")
        shot = await engine.retrieve_one(URL, session, methods=["browser"], output_format="html",
                                         screenshot=True)
    assert plain.screenshot is None
    assert shot.screenshot == "a screenshot" and shot.from_cache  # Shares the retrieval
    assert len(taken) == 1


async def test_no_screenshot_of_a_failed_retrieval(browser_serves):
    taken = browser_serves("")  # Nothing there
    async with aiohttp.ClientSession() as session:
        response = await engine.retrieve_one(URL, session, methods=["browser"],
                                             output_format="html", screenshot=True)
    assert not response.success and response.screenshot is None and not taken


def test_screenshot_travels_over_the_wire():
    descriptor = ItemDescriptor(ref="<image:7>", kind="image", id=7, media_url="/v1/media/image/7")
    payload = ResponsePayload(url=URL, screenshot=descriptor)
    assert ResponsePayload.from_dict(payload.to_dict()).screenshot == descriptor
    assert ResponsePayload.from_dict({"url": URL}).screenshot is None
