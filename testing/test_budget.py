"""The time a browser retrieval has left, which its media wait no longer than."""

import asyncio

import pytest

from scrapemm.server import budget

pytestmark = pytest.mark.server


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(budget.time, "monotonic", lambda: now[0])
    return now


def test_without_a_budget_nothing_is_capped():
    assert budget.remaining() is None
    assert budget.cap(300) == 300
    assert budget.abandoned() == set()


async def test_a_wait_is_capped_by_what_is_left(clock):
    async def retrieval():
        budget.start(120)
        assert budget.cap(300) == 110          # Keeps 10 seconds for what follows
        assert budget.cap(30) == 30            # Plenty left: the wait as asked
        clock[0] += 100                        # 20 seconds are left
        assert budget.cap(300) == 10
        assert budget.cap(300, reserve=30) == 5  # Not less than the floor
        clock[0] += 100                        # Past the end
        assert budget.cap(300) == 5
        assert budget.remaining() < 0

    await asyncio.create_task(retrieval())


async def test_a_budget_belongs_to_its_retrieval(clock):
    async def retrieval(seconds):
        budget.start(seconds)
        await asyncio.sleep(0)
        return budget.remaining()

    first, second = await asyncio.gather(asyncio.create_task(retrieval(120)),
                                         asyncio.create_task(retrieval(30)))
    assert (first, second) == (120, 30)
    assert budget.remaining() is None  # Nor does it leak into the caller


async def test_media_given_up_on_are_remembered_for_the_retrieval(clock):
    async def retrieval():
        budget.start(120)
        budget.abandoned().add("https://example.org/slow.mp4")
        # What the retrieval starts inherits it
        return await asyncio.create_task(asyncio.sleep(0, result=set(budget.abandoned())))

    assert await asyncio.create_task(retrieval()) == {"https://example.org/slow.mp4"}
    assert budget.abandoned() == set()
