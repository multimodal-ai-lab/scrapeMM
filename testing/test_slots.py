"""Each retrieval method's slots and cooldown (`scrapemm.server.slots`): limits, counts,
live changes of the settings, pacing, re-entrancy and cleanup after cancellation."""

import asyncio
import time

import pytest

from scrapemm.server import config, slots, timing

pytestmark = pytest.mark.server


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    """A config of its own for each test, and fresh gates."""
    values: dict = {}
    monkeypatch.setattr(config, "_config", values)
    monkeypatch.setattr(slots, "_gates", {})
    monkeypatch.setattr(slots, "_exemptions", {})
    monkeypatch.setattr(slots, "_fail_fast", {})
    return values


async def _hold_until(key: str, release: asyncio.Event, started: list):
    async with slots.hold(key):
        started.append(time.monotonic())
        await release.wait()


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_limit_and_counts(settings):
    settings["method_slots"] = {"m": 2}
    release, started = asyncio.Event(), []
    tasks = [asyncio.create_task(_hold_until("m", release, started)) for _ in range(3)]
    await _settle()
    snap = slots.gate("m").snapshot()
    assert (snap["limit"], snap["in_use"], snap["waiting"]) == (2, 2, 1)
    assert len(started) == 2
    release.set()
    await asyncio.gather(*tasks)
    snap = slots.gate("m").snapshot()
    assert (snap["in_use"], snap["waiting"], len(started)) == (0, 0, 3)


async def test_defaults_and_overrides(settings):
    assert slots.limit_of("browser") == slots.DEFAULT_SLOTS["browser"]
    assert slots.limit_of("unheard-of") == slots.FALLBACK_SLOTS
    assert slots.cooldown_of("youtube") == slots.DEFAULT_COOLDOWNS["youtube"]
    assert slots.cooldown_of("browser") == 0
    settings["youtube_min_interval"] = 5  # A config from before
    assert slots.cooldown_of("youtube") == 5
    settings["method_cooldowns"] = {"youtube": 7}  # The new setting wins
    assert slots.cooldown_of("youtube") == 7


async def test_raised_limit_applies_to_waiters(settings):
    settings["method_slots"] = {"m": 1}
    release, started = asyncio.Event(), []
    tasks = [asyncio.create_task(_hold_until("m", release, started)) for _ in range(3)]
    await _settle()
    assert len(started) == 1 and slots.gate("m").waiting == 2
    settings["method_slots"] = {"m": 3}
    slots.refresh()
    await _settle()
    assert len(started) == 3 and slots.gate("m").waiting == 0
    release.set()
    await asyncio.gather(*tasks)


async def test_lowered_limit_lets_running_finish(settings):
    settings["method_slots"] = {"m": 2}
    release, started = asyncio.Event(), []
    tasks = [asyncio.create_task(_hold_until("m", release, started)) for _ in range(2)]
    await _settle()
    settings["method_slots"] = {"m": 1}
    slots.refresh()
    late = asyncio.create_task(_hold_until("m", asyncio.Event(), started))
    await _settle()
    assert slots.gate("m").snapshot()["in_use"] == 2 and len(started) == 2
    release.set()
    await asyncio.gather(*tasks)
    await _settle()
    assert len(started) == 3  # Under the new limit, once the others left
    late.cancel()
    await asyncio.gather(late, return_exceptions=True)
    assert slots.gate("m").in_use == 0


async def test_cooldown_spaces_starts(settings):
    settings["method_cooldowns"] = {"m": 0.15}
    release, started = asyncio.Event(), []
    release.set()
    await asyncio.gather(*(_hold_until("m", release, started) for _ in range(3)))
    gaps = [b - a for a, b in zip(started, started[1:])]
    assert all(0.14 <= gap <= 0.15 * (1 + slots.COOLDOWN_JITTER) + 0.1 for gap in gaps), gaps
    assert slots.gate("m").snapshot()["next_start"] is not None


async def test_cooling_waiter_holds_no_slot_and_blocks_no_other_key(settings):
    settings["method_cooldowns"] = {"m": 0.3}
    release, started = asyncio.Event(), []
    release.set()
    await _hold_until("m", release, started)
    waiter = asyncio.create_task(_hold_until("m", release, started))
    await _settle()
    snap = slots.gate("m").snapshot()
    assert (snap["in_use"], snap["waiting"]) == (0, 1)
    other = []
    await asyncio.wait_for(_hold_until("n", release, other), 0.1)  # Not held up
    assert other
    await waiter


async def test_exempt_starts_skip_the_cooldown(settings):
    settings["method_cooldowns"] = {"m": 10}
    exempt = [False]
    slots.exempt_from_cooldown("m", lambda: exempt[0])
    release, started = asyncio.Event(), []
    release.set()
    await _hold_until("m", release, started)
    waiter = asyncio.create_task(_hold_until("m", release, started))
    await _settle()
    assert slots.gate("m").waiting == 1
    exempt[0] = True
    slots.wake("m")  # As YouTube does when it pauses
    await asyncio.wait_for(waiter, 1)
    assert len(started) == 2


async def test_same_key_is_reentrant(settings):
    settings["method_slots"] = {"m": 1}

    async def nested():
        async with slots.hold("m"):
            async with slots.hold("m"):  # Would deadlock if it waited
                assert slots.gate("m").in_use == 1
            async with slots.hold("n"):  # Another key: taken as well
                assert slots.gate("n").in_use == 1

    await asyncio.wait_for(nested(), 1)
    assert slots.gate("m").in_use == 0 and slots.gate("n").in_use == 0


async def test_spawned_tasks_inherit_held_slots(settings):
    settings["method_slots"] = {"m": 1}

    async def child():
        async with slots.hold("m"):
            return True

    async with slots.hold("m"):
        assert await asyncio.wait_for(asyncio.create_task(child()), 1)
    # Released by its owner; a new task waits for it as usual
    assert slots.gate("m").in_use == 0


async def test_cancellation_cleans_up(settings):
    settings["method_slots"] = {"m": 1}
    release, started = asyncio.Event(), []
    holder = asyncio.create_task(_hold_until("m", release, started))
    waiter = asyncio.create_task(_hold_until("m", release, started))
    await _settle()
    assert slots.gate("m").waiting == 1
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert (slots.gate("m").in_use, slots.gate("m").waiting) == (1, 0)
    holder.cancel()
    await asyncio.gather(holder, return_exceptions=True)
    assert slots.gate("m").in_use == 0
    release.set()
    await asyncio.wait_for(_hold_until("m", release, started), 1)


async def test_first_slot_admits_the_url(settings):
    settings["method_slots"] = {"m": 1}
    release, started = asyncio.Event(), []
    holder = asyncio.create_task(_hold_until("m", release, started))
    await _settle()
    clocks = []

    async def retrieval():
        clocks.append(timing.start_clock())
        await _hold_until("m", release, started)  # The first method's slot
        await _hold_until("n", release, started)  # A later one's: no effect

    task = asyncio.create_task(retrieval())
    await _settle()
    clock = clocks[0]
    assert clock.admitted is None and clock.waiting_for_slot == 1
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(holder, task)
    assert clock.admitted is not None and clock.waiting_for_slot == 0
    assert clock.admitted - clock.entered >= 0.05


async def test_merge_validates_and_resets(monkeypatch):
    monkeypatch.setattr(slots, "keys", lambda: ["youtube", "browser"])
    merged = slots.merge("method_slots", {"browser": 4}, {"youtube": "2"})
    assert merged == {"browser": 4, "youtube": 2}
    assert slots.merge("method_slots", merged, {"browser": None}) == {"youtube": 2}
    assert slots.merge("method_cooldowns", None, {"youtube": "1.5"}) == {"youtube": 1.5}
    for setting, changes in (("method_slots", {"nope": 1}), ("method_slots", {"browser": 0}),
                             ("method_slots", {"browser": 1.5}), ("method_slots", {"browser": True}),
                             ("method_cooldowns", {"youtube": -1}),
                             ("method_cooldowns", {"youtube": "soon"}), ("method_slots", [1])):
        with pytest.raises(ValueError):
            slots.merge(setting, {}, changes)


def test_keys_and_methods():
    keys = slots.keys()
    assert {"youtube", "facebook", "browser", "firecrawl", "decodo", "plain_http",
            "perma_cc", "wayback"} <= set(keys)
    methods = {m["key"]: m for m in slots.methods()}
    assert methods["youtube"]["kind"] == "integration"
    assert methods["wayback"]["kind"] == "archive"
    assert methods["plain_http"]["kind"] == "method"
    assert methods["youtube"]["cooldown_default"] == 12.0
    assert set(slots.snapshot()) >= set(keys)


def test_schema_covers_every_setting():
    entries = config.schema()
    assert [e["key"] for e in entries if e["key"] not in config.SETTINGS] == []
    assert {e["key"] for e in entries} == set(config.SETTINGS)
    sections = {"general", "chain", "site-rules", "parallelism", "browser", "integrations",
                "archives", "cache", "media", "history", "secrets", "proxy"}
    for e in entries:
        assert e["section"] in sections, e
        assert e["type"] == config.SETTINGS[e["key"]].__name__, e
        assert e["label"] and e["description"], e


async def test_a_hopeless_retrieval_fails_at_once_instead_of_waiting(settings):
    """YouTube, while it refuses this server and the proxy: its URLs must not queue for
    the slot only to fail (with one slot, they waited up to 33 s each)."""
    settings["method_slots"] = {"m": 1}
    hopeless = []

    def check():
        if hopeless:
            raise RuntimeError("would fail anyway")
    slots.fail_fast("m", check)
    release, started = asyncio.Event(), []
    holder = asyncio.create_task(_hold_until("m", release, started))
    waiter = asyncio.create_task(_hold_until("m", release, started))
    await _settle()
    assert slots.gate("m").waiting == 1
    hopeless.append(True)
    slots.wake("m")  # As the YouTube pause does when it learns the proxy is refused too
    await _settle()
    with pytest.raises(RuntimeError):
        await waiter  # Failed while waiting, without the slot
    assert slots.gate("m").waiting == 0 and slots.gate("m").in_use == 1
    with pytest.raises(RuntimeError):
        async with slots.hold("m"):  # A new one fails before it waits at all
            pass
    release.set()
    await holder
