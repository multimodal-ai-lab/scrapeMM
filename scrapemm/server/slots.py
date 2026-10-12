"""Slots: how many retrievals each method runs at once, and how fast they may start.

The server used to have one limit on URLs in flight (40). One limit for everything let a
slow method crowd out the rest: a batch of YouTube URLs, paced at one every 12 s, held
its places for minutes while open-web URLs that the browser would have answered in
seconds waited behind them. Now every retrieval method has its own gate:

* **Slots** (`method_slots`): how many retrievals by the method run at once; more wait.
* **Cooldown** (`method_cooldowns`): the seconds between the starts of two retrievals by
  the method, with up to +50% jitter (a fixed rhythm is a bot's signature). YouTube needs
  it: it starts its bot check after bursts.

A URL waiting for one method's slot holds no other slot and blocks no other method. A
waiter does not hold a slot while it waits out a cooldown either.

The gates are keyed like the dashboard's cards: an integration by its name in lower case
("facebook", "x (twitter)"), the general methods as "browser", "firecrawl", "decodo",
plus the steps of the retrieval chain that have no card: "plain_http", and the archive
methods "perma_cc" and "wayback". Search providers have their own limit
(`max_search_concurrency`, see `search.base`).

Nesting
-------
Some methods use others internally: Instagram, TikTok and Facebook fall back to Decodo,
and any page may embed a YouTube video that is downloaded along with it. Decodo and
yt-dlp's YouTube downloads take their slot themselves, so the limit holds for them
wherever they are called from:

* A task that already holds a key's slot passes straight through when it asks for that
  key again (the engine takes Decodo's slot, then `decodo.scrape()` asks once more).
  Waiting for its own slot would be a deadlock.
* A task holding one key that asks for *another* waits for it like anybody else, keeping
  the slot it holds. That cannot deadlock as long as the nesting has a direction, which
  it has: any key -> "decodo" -> "youtube" (a page Decodo got may embed a YouTube video),
  and nothing is taken inside "youtube". Whoever adds a nested acquisition keeps it so.

The browser's pages are not slotted when another method uses them (the archive
integrations replay in it): the browser's own page limit (`max_browser_pages`) covers
every page, whoever opens it.

Why not asyncio.Semaphore: its limit is fixed, so a new limit would mean a new
semaphore, and the URLs waiting on the old one would be lost to it; nor does it say how
many wait. Here the limit is read from the settings at every turn, waiters are woken
when it changes (`refresh()`), and releasing is synchronous, so that a cancelled task
frees its slot in its `finally` without awaiting anything.
"""

import asyncio
import random
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Callable, Optional

from .config import get_config_var
from . import timing

SLOTS_KEY = "method_slots"
COOLDOWNS_KEY = "method_cooldowns"

# Slots per method by default. The former single limit was 40 URLs in flight, each
# trying one method at a time, so a method could have up to 40 retrievals running; the
# defaults keep that order of magnitude where a method can take it, and go lower where
# the other side limits us:
# * browser: as many as the browser opens pages (`DEFAULT_MAX_BROWSER_PAGES` in
#   `integrations.headed_browser`); more would only wait for a page there.
# * plain_http: requests that cost next to nothing, as before.
# * firecrawl, decodo: services with a concurrency limit of their own (a self-hosted
#   Firecrawl's workers, Decodo's plan); more at once only queue there, or fail.
# * youtube: paced by its cooldown anyway (one start every 12-18 s); a few at once let
#   a slow download not hold up the next start.
# * archive.today, ghostarchive, perma_cc, wayback: archives that answer bursts with
#   CAPTCHAs or HTTP 429. The engine's Wayback lookups were already limited to 2 at once
#   (`engine._wayback_gate`), Perma.cc's to 2 (`perma_cc.LOOKUP_CONCURRENCY`).
# * Every other integration: FALLBACK_SLOTS. Platform APIs rate-limit per account, and
#   TikTok keeps its Research API to 5 queries at once internally besides.
DEFAULT_SLOTS: dict[str, int] = {
    "browser": 32,
    "plain_http": 40,
    "firecrawl": 20,
    "decodo": 20,
    "youtube": 4,
    "archive.today": 4,
    "ghostarchive": 4,
    "megalodon": 4,
    "perma_cc": 4,
    "wayback": 4,
}
FALLBACK_SLOTS = 16

# Seconds between two starts by default: YouTube's former `youtube_min_interval`. Its
# documented guest limit is about 300 videos an hour; 12 s plus jitter stays under it.
DEFAULT_COOLDOWNS: dict[str, float] = {"youtube": 12.0}
COOLDOWN_JITTER = 0.5  # Up to +50% on top of every cooldown


def keys() -> list[str]:
    """Every slotted method, in the dashboard's order: the integrations, the general
    methods, then the chain's steps without a card."""
    from .integrations import RETRIEVAL_INTEGRATIONS
    return ([i.name.lower() for i in RETRIEVAL_INTEGRATIONS]
            + ["browser", "firecrawl", "decodo", "plain_http", "perma_cc", "wayback"])


def default_slots(key: str) -> int:
    return DEFAULT_SLOTS.get(key, FALLBACK_SLOTS)


def default_cooldown(key: str) -> float:
    return DEFAULT_COOLDOWNS.get(key, 0.0)


def limit_of(key: str) -> int:
    """The method's slots: the setting, else the default."""
    configured = (get_config_var(SLOTS_KEY) or {}).get(key)
    return max(1, int(configured)) if configured is not None else default_slots(key)


def cooldown_of(key: str) -> float:
    """The method's cooldown in seconds (0: none): the setting, else the default. A
    config from before still has YouTube's pace as `youtube_min_interval`."""
    configured = (get_config_var(COOLDOWNS_KEY) or {}).get(key)
    if configured is None and key == "youtube":
        configured = get_config_var("youtube_min_interval")
    return max(0.0, float(configured)) if configured is not None else default_cooldown(key)


# Keys whose cooldown does not apply at the moment, by a predicate asked in the waiting
# task's own context (see `exempt_from_cooldown()`)
_exemptions: dict[str, Callable[[], bool]] = {}


# Checks that fail a method's retrieval at once, before it waits for a slot (see
# `fail_fast()`)
_fail_fast: dict[str, Callable[[], None]] = {}


def fail_fast(key: str, check: Callable[[], None]) -> None:
    """Has `check()` asked before a retrieval by the method waits for a slot and on every
    wake-up while it waits: if it raises, the retrieval fails with that at once, instead
    of waiting for a slot and a cooldown to learn the same (YouTube, while it refuses this
    server and the proxy alike)."""
    _fail_fast[key] = check


def exempt_from_cooldown(key: str, predicate: Callable[[], bool]) -> None:
    """Lets the method start without its cooldown whenever `predicate()` says so, asked in
    the waiting task: YouTube through the proxy, whose address is not the one YouTube
    paces us for. Such a start does not delay the next paced one either."""
    _exemptions[key] = predicate


class Gate:
    """One method's slots: a counter of those in use and the tasks waiting for one."""

    def __init__(self, key: str):
        self.key = key
        self.in_use = 0
        self.waiting = 0
        self.next_start = 0.0  # UNIX time before which no paced start may happen
        self._wakeups: list[asyncio.Future] = []  # One per waiting task, in arrival order

    def _paced(self) -> bool:
        exempt = _exemptions.get(self.key)
        return cooldown_of(self.key) > 0 and not (exempt is not None and exempt())

    async def acquire(self) -> None:
        """Waits until a slot is free and, unless exempt, the cooldown is over; then takes
        the slot. Counts the wait as the URL's queue time if it is its first slot (see
        `timing`)."""
        clock = timing.current()
        first = clock is not None and clock.admitted is None
        since = time.time()
        if check := _fail_fast.get(self.key):
            check()
        if self.in_use >= limit_of(self.key) or (self._paced() and self.next_start > since):
            self.waiting += 1
            if first:
                clock.waiting_for_slot += 1
            try:
                await self._wait()
            finally:
                self.waiting -= 1
                if first:
                    clock.waiting_for_slot -= 1
        self.in_use += 1
        if self._paced():
            self.next_start = time.time() + cooldown_of(self.key) * random.uniform(
                1.0, 1.0 + COOLDOWN_JITTER)
        if first and clock.admitted is None:  # Not admitted meanwhile by a hedged sibling
            clock.admit()
            # The host's reachability check may have started the clock already: the
            # wait for the first method is still queue time
            timing.wait_for_slot(time.time() - since)

    async def _wait(self) -> None:
        """Sleeps until woken (a slot freed, the settings changed) or the cooldown is
        over, until the slot can be taken. Every waiter re-checks on every wake-up, in
        the order they came: a waiter that cannot start yet (its cooldown) must not keep
        one that can (exempt from it) waiting."""
        loop = asyncio.get_running_loop()
        while True:
            now = time.time()
            if self.in_use < limit_of(self.key):
                delay = self.next_start - now if self._paced() else 0.0
                if delay <= 0:
                    return
            else:
                delay = None  # Until a slot is freed
            wakeup = loop.create_future()
            self._wakeups.append(wakeup)
            try:
                await asyncio.wait_for(wakeup, delay)
            except asyncio.TimeoutError:
                pass
            finally:
                if wakeup in self._wakeups:
                    self._wakeups.remove(wakeup)
            if check := _fail_fast.get(self.key):
                check()  # It may have become hopeless while it waited

    def release(self) -> None:
        self.in_use -= 1
        self.wake()

    def wake(self) -> None:
        """Lets every waiter check again."""
        wakeups, self._wakeups = self._wakeups, []
        for wakeup in wakeups:
            if not wakeup.done():
                wakeup.set_result(None)

    def snapshot(self) -> dict:
        return {"limit": limit_of(self.key), "in_use": self.in_use, "waiting": self.waiting,
                "cooldown": cooldown_of(self.key),
                "next_start": self.next_start if self.next_start > time.time() else None}


_gates: dict[str, Gate] = {}

# The keys whose slot the current task (and the tasks it spawned) holds
_held: ContextVar[frozenset[str]] = ContextVar("held_slots", default=frozenset())


def gate(key: str) -> Gate:
    found = _gates.get(key)
    if found is None:
        found = _gates[key] = Gate(key)
    return found


@asynccontextmanager
async def hold(key: str):
    """Runs the block in one of the method's slots; see the module docstring for what
    nesting does."""
    held = _held.get()
    if key in held:
        yield
        return
    g = gate(key)
    await g.acquire()
    _held.set(held | {key})
    try:
        yield
    finally:
        _held.set(held)
        g.release()


def refresh() -> None:
    """Wakes every waiter to apply changed settings: a raised limit lets more start at
    once, a shorter cooldown sooner."""
    for g in _gates.values():
        g.wake()


def wake(key: str) -> None:
    """Wakes the method's waiters to check again, e.g. because an exemption now holds."""
    if key in _gates:
        _gates[key].wake()


def snapshot() -> dict[str, dict]:
    """Each method's limit, slots in use, waiters, cooldown and next allowed start (UNIX
    time, or None), for the dashboard."""
    return {key: gate(key).snapshot() for key in dict.fromkeys(keys() + list(_gates))}


def methods() -> list[dict]:
    """Every slotted method with its effective and default slots and cooldown, for the
    settings' parallelism editor."""
    from .integrations import RETRIEVAL_INTEGRATIONS
    from .chain import METHOD_INFO
    integrations = {i.name.lower(): i.name for i in RETRIEVAL_INTEGRATIONS}
    result = []
    for key in keys():
        if key in integrations:
            name, kind = integrations[key], "integration"
        elif METHOD_INFO[key].stage == "archive":
            name, kind = METHOD_INFO[key].label, "archive"  # "Perma.cc archive": not the integration
        else:
            name, kind = METHOD_INFO[key].name, "method"
        result.append({"key": key, "name": name, "kind": kind,
                       "slots": limit_of(key), "slots_default": default_slots(key),
                       "cooldown": cooldown_of(key), "cooldown_default": default_cooldown(key)})
    return result


def merge(setting: str, current: Optional[dict], changes) -> dict:
    """The overrides of `setting` (`method_slots` or `method_cooldowns`) after applying
    `changes`: {key: value} sets a key, {key: None} resets it to its default. Only
    overrides are stored. Raises ValueError, naming the culprit, on unknown keys or
    invalid values."""
    if not isinstance(changes, dict):
        raise ValueError(f"'{setting}' expects an object of method: value, got {changes!r}.")
    known = set(keys())
    merged = dict(current or {})
    for key, value in changes.items():
        if key not in known:
            raise ValueError(f"'{setting}': unknown method '{key}'. Known: {', '.join(sorted(known))}.")
        if value is None:
            merged.pop(key, None)
            continue
        try:
            if setting == SLOTS_KEY:
                if isinstance(value, bool) or float(value) != int(float(value)):
                    raise ValueError
                value = int(float(value))
                if value < 1:
                    raise ValueError
            else:
                value = float(value)
                if not value >= 0 or value == float("inf"):
                    raise ValueError
        except (TypeError, ValueError):
            expected = "a whole number of at least 1" if setting == SLOTS_KEY \
                else "a number of seconds of at least 0"
            raise ValueError(f"'{setting}' for '{key}' expects {expected}, got {value!r}.")
        merged[key] = value
    return merged
