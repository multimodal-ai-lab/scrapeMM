"""A frame that does not answer must not hold the retrieval: perma.cc/75EG-E5GK waited
two minutes (ten, before the limit was lowered) in a call to a frame busy with a video."""

import asyncio
import time

import pytest
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from scrapemm.server.integrations.headed_browser import evaluate_within

pytestmark = pytest.mark.server


class Frame:
    def __init__(self, answer=None, delay=0.0):
        self.answer, self.delay, self.calls = answer, delay, []
        self.url = "https://example.org/frame"

    async def evaluate(self, script, arg=None):
        self.calls.append((script, arg))
        await asyncio.sleep(self.delay)
        return self.answer


async def test_an_answer_in_time_is_returned():
    frame = Frame(answer=42, delay=0.01)
    assert await evaluate_within(frame, "() => 42", timeout=1) == 42
    assert await evaluate_within(frame, "x => x", {"a": 1}, timeout=1) == 42
    assert frame.calls == [("() => 42", None), ("x => x", {"a": 1})]


async def test_a_frame_that_does_not_answer_raises_playwrights_timeout():
    started = time.monotonic()
    with pytest.raises(PlaywrightTimeoutError):
        await evaluate_within(Frame(delay=30), "() => 1", timeout=0.2)
    assert time.monotonic() - started < 2
    # Which callers that expect a failed evaluation catch as any other
    assert issubclass(PlaywrightTimeoutError, PlaywrightError)


async def test_the_media_frame_is_searched_without_adding_up_the_waits(monkeypatch):
    from scrapemm.server.integrations import perma_cc

    class Page:
        def __init__(self, frames):
            self.frames = frames

    root = Frame(answer=0)
    silent = [Frame(delay=30) for _ in range(5)]
    root.page = Page([root, *silent])
    for frame in silent:
        frame.parent_frame = root
        frame.page = root.page
    root.parent_frame = None

    async def score(frame):
        try:
            return int(await evaluate_within(frame, "() => 1", timeout=0.3))
        except Exception:
            return 0

    monkeypatch.setattr(perma_cc.PermaCC, "_media_score", staticmethod(score))
    started = time.monotonic()
    best = await perma_cc.PermaCC()._pick_best_media_frame(root)
    assert best is root
    assert time.monotonic() - started < 1.2  # Five silent frames: one wait, not five
