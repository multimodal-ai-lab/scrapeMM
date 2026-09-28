"""Watches the event loop for stalls.

The server is one process with one event loop: anything that holds it -- a synchronous
call, CPU-bound work, a thread hogging the GIL -- stalls every request at once, the
health check and the streamed results included. A client then sees nothing for as long
as the stall lasts. This measures how late a short sleep wakes up, which is exactly
that delay, logs stalls worth knowing about, and keeps the recent worst case for
/healthz.

A watchdog thread also notices a stall while it lasts and records where the loop's
thread is at that moment, so the log says what held it rather than just that something
did.
"""

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from typing import Optional

from scrapemm.common.paths import APP_NAME

logger = logging.getLogger(APP_NAME)

INTERVAL = 0.5  # Seconds between two measurements
WARN_AFTER = 1.0  # A stall this long is logged
WINDOW = 60.0  # Seconds that `recent_max()` looks back
STACK_EVERY = float(os.getenv("SCRAPEMM_STALL_STACK_EVERY", 60))  # At most one stack per this many s

_samples: deque[tuple[float, float]] = deque()  # (time, lag)
_last_tick = time.monotonic()
_stall_stack: Optional[str] = None  # Where the loop's thread was during the current stall


async def watch() -> None:
    global _last_tick, _stall_stack
    loop = asyncio.get_running_loop()
    threading.Thread(target=_watchdog, args=(threading.get_ident(),),
                     name="loop-watchdog", daemon=True).start()
    while True:
        before = loop.time()
        _last_tick = time.monotonic()
        await asyncio.sleep(INTERVAL)
        lag = loop.time() - before - INTERVAL
        _last_tick = time.monotonic()
        now = time.time()
        _samples.append((now, lag))
        while _samples and _samples[0][0] < now - WINDOW:
            _samples.popleft()
        if lag >= WARN_AFTER:
            stack, _stall_stack = _stall_stack, None
            logger.warning(f"⏱️ The event loop stalled for {lag:.1f} s: every request, the "
                           f"streamed results included, waited that long."
                           + (f" It was held here:\n{stack}" if stack else ""))


def _watchdog(loop_thread: int) -> None:
    """Samples the loop thread's stack once a stall has lasted WARN_AFTER seconds."""
    global _stall_stack
    last_stack = 0.0
    while True:
        time.sleep(0.25)
        if time.monotonic() - _last_tick < INTERVAL + WARN_AFTER:
            continue
        if _stall_stack is not None or time.monotonic() - last_stack < STACK_EVERY:
            continue
        frames = sys._current_frames()
        frame = frames.get(loop_thread)
        if frame is None:
            continue
        stack = "".join(traceback.format_stack(frame)[-8:])
        # The loop may also just be starved of the GIL by another thread: list the threads
        # that were busy (not waiting on a lock, a queue or a socket), innermost frames last
        names = {t.ident: t.name for t in threading.enumerate()}
        busy = []
        for ident, other in frames.items():
            if ident in (loop_thread, threading.get_ident()):
                continue
            inner = traceback.format_stack(other)[-3:]
            if inner and any(idle in inner[-1] for idle in (
                    "threading.py", "queue.py", "selectors.py", "thread.py")):
                continue
            busy.append(f"[{names.get(ident, ident)}]\n" + "".join(inner))
        if busy:
            stack += "Other busy threads:\n" + "\n".join(busy)
        _stall_stack = stack
        last_stack = time.monotonic()


def recent_max() -> float:
    """The longest stall of the last WINDOW seconds, in seconds."""
    return max((lag for _, lag in _samples), default=0.0)
