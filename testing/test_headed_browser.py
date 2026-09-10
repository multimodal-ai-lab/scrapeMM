import asyncio
from unittest.mock import MagicMock, patch

import pytest
from playwright._impl._errors import TargetClosedError
from playwright.async_api import Error as PlaywrightError

from scrapemm.integrations.headed_browser import HeadedBrowser
from scrapemm.integrations.perma_cc import PermaCC


@pytest.fixture(autouse=True)
def reset_headed_browser():
    HeadedBrowser._browser = None
    HeadedBrowser._generation = 0
    HeadedBrowser._lock = asyncio.Lock()
    yield
    if HeadedBrowser._browser:
        try:
            HeadedBrowser._browser.quit()
        except Exception:
            pass
    HeadedBrowser._browser = None
    HeadedBrowser._generation = 0


def make_mock_browser(stopped: bool = False):
    browser = MagicMock()
    browser.stopped = stopped
    browser.get_endpoint_url.return_value = "ws://127.0.0.1:9222"
    return browser


@pytest.mark.asyncio
async def test_concurrent_connect_opens_one_browser():
    """Many concurrent _connect() calls must only start the shared browser once."""
    integration = PermaCC()
    integration.connected = None
    mock_browser = make_mock_browser()
    start_calls = 0

    async def fake_start_async(**_kwargs):
        nonlocal start_calls
        start_calls += 1
        await asyncio.sleep(0.05)
        return mock_browser

    with patch(
        "scrapemm.integrations.headed_browser.cdp_driver.start_async",
        side_effect=fake_start_async,
    ):
        await asyncio.gather(*[integration._connect() for _ in range(13)])

    assert start_calls == 1
    assert HeadedBrowser._browser is mock_browser
    assert HeadedBrowser._generation == 1
    assert integration.connected is True


@pytest.mark.asyncio
async def test_ensure_browser_single_flight_recovery_on_crash():
    """When many concurrent tasks report the same dead generation, the browser is
    restarted exactly once, and everyone ends up on the fresh instance."""
    integration = PermaCC()

    browsers = [make_mock_browser() for _ in range(2)]
    start_calls = 0

    async def fake_start_async(**_kwargs):
        nonlocal start_calls
        browser = browsers[start_calls]
        start_calls += 1
        await asyncio.sleep(0.02)
        return browser

    with patch(
        "scrapemm.integrations.headed_browser.cdp_driver.start_async",
        side_effect=fake_start_async,
    ):
        # Establish the initial shared browser (generation 0 -> 1).
        browser0, gen0 = await integration._ensure_browser()
        assert browser0 is browsers[0]
        assert gen0 == 1

        # Simulate several concurrent tasks all discovering that generation 1 is dead
        # at roughly the same time.
        results = await asyncio.gather(
            *[integration._ensure_browser(bad_generation=gen0) for _ in range(10)]
        )

    # Only one restart should have happened, despite 10 concurrent crash reports.
    assert start_calls == 2
    assert HeadedBrowser._generation == 2
    for browser, generation in results:
        assert browser is browsers[1]
        assert generation == 2


@pytest.mark.asyncio
async def test_ensure_browser_restarts_when_stopped():
    """A browser whose underlying process died (stopped=True) is proactively restarted
    even without an explicit bad_generation report."""
    integration = PermaCC()

    dead_browser = make_mock_browser(stopped=True)
    fresh_browser = make_mock_browser()
    HeadedBrowser._browser = dead_browser
    HeadedBrowser._generation = 3

    with patch(
        "scrapemm.integrations.headed_browser.cdp_driver.start_async",
        return_value=fresh_browser,
    ):
        browser, generation = await integration._ensure_browser()

    assert browser is fresh_browser
    assert generation == 4


@pytest.mark.asyncio
async def test_get_recovers_from_browser_crash_and_retries_once():
    """A crash detected during page.goto() triggers exactly one recovery + retry, and
    the call ultimately succeeds on the fresh browser."""
    integration = PermaCC()

    good_browser = make_mock_browser()
    start_calls = 0

    async def fake_start_async(**_kwargs):
        nonlocal start_calls
        start_calls += 1
        return good_browser

    class FakePage:
        def __init__(self, should_crash: bool):
            self.should_crash = should_crash
            self.context = MagicMock(request=MagicMock())
            self.closed = False

        async def set_viewport_size(self, *_a, **_kw):
            return None

        async def goto(self, *_a, **_kw):
            if self.should_crash:
                raise TargetClosedError("Target page, context or browser has been closed")
            return None

        async def close(self):
            self.closed = True

    pages = [FakePage(should_crash=True), FakePage(should_crash=False)]
    new_page_calls = 0

    async def fake_new_page(_p, attempts: int = 3):
        nonlocal new_page_calls
        page = pages[new_page_calls]
        generation = new_page_calls
        new_page_calls += 1
        return page, generation

    async def fake_extract_content(_page):
        return _page

    async def fake_html_and_source(_target, _page):
        return "<html>ok</html>", _page

    async def fake_to_multimodal_sequence(html, **_kwargs):
        return html

    with patch(
        "scrapemm.integrations.headed_browser.cdp_driver.start_async",
        side_effect=fake_start_async,
    ), patch.object(HeadedBrowser, "_new_page", side_effect=fake_new_page), \
         patch.object(PermaCC, "_extract_content", side_effect=fake_extract_content), \
         patch.object(HeadedBrowser, "_html_and_source", side_effect=fake_html_and_source), \
         patch("scrapemm.util.to_multimodal_sequence", side_effect=fake_to_multimodal_sequence):
        result = await integration._get("https://perma.cc/AAAA-BBBB")

    assert result.html == "<html>ok</html>"
    assert result.multimodal == "<html>ok</html>"
    assert new_page_calls == 2  # one crashed attempt + one successful retry
    assert pages[0].closed
    assert pages[1].closed


@pytest.mark.asyncio
async def test_get_raises_instead_of_returning_none_when_browser_unavailable():
    """A HeadedBrowser subclass's get() must never return None; if the shared browser
    cannot be started at all, it must raise an informative exception instead."""
    integration = PermaCC()

    with patch(
        "scrapemm.integrations.headed_browser.cdp_driver.start_async",
        return_value=None,
    ):
        with pytest.raises(RuntimeError):
            await integration.get("https://perma.cc/AAAA-BBBB")


@pytest.mark.asyncio
async def test_transient_connect_failure_does_not_permanently_block_future_calls():
    """A failed shared-browser startup must not be cached forever: get() bypasses the
    generic connected-cache, so a later, successful attempt must still work."""
    integration = PermaCC()

    call_count = 0

    async def flaky_start_async(**_kwargs):
        nonlocal call_count
        call_count += 1
        # Fail for all 3 internal attempts of the first get() call, then recover.
        if call_count <= 3:
            return None
        return make_mock_browser()

    class FakePage:
        def __init__(self):
            self.context = MagicMock(request=MagicMock())

        async def set_viewport_size(self, *_a, **_kw):
            return None

        async def goto(self, *_a, **_kw):
            return None

        async def close(self):
            return None

    with patch(
        "scrapemm.integrations.headed_browser.cdp_driver.start_async",
        side_effect=flaky_start_async,
    ):
        # First call: browser fails to start on every attempt -> informative error, not None.
        with pytest.raises(RuntimeError):
            await integration.get("https://perma.cc/AAAA-BBBB")

        # Second call: browser starts fine now. Must succeed, proving the earlier failure
        # (which left self.connected as None, not False) did not permanently disable retries.
        with patch.object(PermaCC, "_extract_content", side_effect=lambda page: page), \
             patch.object(HeadedBrowser, "_html_and_source", side_effect=lambda _t, _p: ("<html>ok</html>", _t)), \
             patch("scrapemm.util.to_multimodal_sequence", side_effect=lambda html, **_kw: html), \
             patch.object(HeadedBrowser, "_new_page", side_effect=lambda _p, attempts=3: (FakePage(), 1)):
            result = await integration.get("https://perma.cc/AAAA-BBBB")
            assert result.html == "<html>ok</html>"


@pytest.mark.asyncio
async def test_get_survives_client_side_redirect_abort():
    """A page.goto() failing with net::ERR_ABORTED (the page redirected itself before our
    wait_until condition fired, e.g. Wayback SPA snapshots) must not be treated as a crash
    or bubble up as a failure — the retrieval should continue on the redirected page."""
    integration = PermaCC()

    class FakePage:
        def __init__(self):
            self.context = MagicMock(request=MagicMock())
            self.url = "https://web.archive.org/web/redirected-target"
            self.settled = False

        async def set_viewport_size(self, *_a, **_kw):
            return None

        async def goto(self, *_a, **_kw):
            raise PlaywrightError(
                'Page.goto: net::ERR_ABORTED at "https://example.com"\n'
                'Call log:\n  - navigating to "https://example.com", waiting until "domcontentloaded"'
            )

        async def wait_for_load_state(self, *_a, **_kw):
            self.settled = True

        async def close(self):
            return None

    page = FakePage()

    with patch.object(HeadedBrowser, "_new_page", side_effect=lambda _p, attempts=3: (page, 1)), \
         patch.object(PermaCC, "_extract_content", side_effect=lambda _page: _page), \
         patch.object(HeadedBrowser, "_html_and_source", side_effect=lambda _t, _p: ("<html>ok</html>", _t)), \
         patch("scrapemm.util.to_multimodal_sequence", side_effect=lambda html, **_kw: html):
        result = await integration.get("https://perma.cc/AAAA-BBBB")

    assert result.html == "<html>ok</html>"
    assert page.settled is True
