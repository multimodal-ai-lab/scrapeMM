"""The live dashboard: one shared loop that keeps the statuses current and pushes what
changed to every open dashboard.

The work is done once per server, not once per viewer. The loop runs only while at
least one dashboard is connected, and every subscriber gets the same messages. Each
piece of the picture is refreshed on its own terms (see `status`): integrations are
re-probed when their result expires, job figures are re-queried after the history
changed, and the media tree is walked again only after new retrievals. A message goes
out only when its content actually differs from what was sent last.
"""

import asyncio
import json
import logging
import time
from typing import AsyncIterator, Optional

from scrapemm.common.paths import APP_NAME
from . import status

logger = logging.getLogger(APP_NAME)

# How often the cheap parts (in-memory counters, cached aggregates) are looked at
TICK = 2.0

# Proxies drop connections that stay silent; a keep-alive line prevents that, and it
# is also what reveals a viewer who has gone away without closing the connection
HEARTBEAT = 15.0


class LiveHub:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()
        self._last: dict[str, bytes] = {}  # Message key -> the line last sent for it
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._refreshing: dict[str, asyncio.Task] = {}
        status._listeners.append(self.wake)

    def wake(self) -> None:
        """Something changed: look now rather than at the next tick."""
        self._wake.set()

    async def subscribe(self) -> AsyncIterator[bytes]:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        try:
            yield _line({"type": "header", "methods": status.all_methods()})
            # What is already known, so a new viewer does not wait for the next change
            for line in list(self._last.values()):
                yield line
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=HEARTBEAT)
                except asyncio.TimeoutError:
                    yield _line({"type": "ping", "at": time.time()})
        finally:
            self._subscribers.discard(queue)
            if not self._subscribers and self._task is not None:
                self._task.cancel()
                self._task = None

    async def _run(self) -> None:
        try:
            while True:
                self._wake.clear()
                try:
                    await self._update()
                except Exception:
                    logger.debug("Updating the live dashboard failed.", exc_info=True)
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=TICK)
                except asyncio.TimeoutError:
                    pass
        finally:
            for task in self._refreshing.values():
                task.cancel()
            self._refreshing.clear()

    async def _update(self) -> None:
        self._publish("environment", {"type": "environment",
                                      "payload": await status.environment()})

        counts = status.retrieval_counts()
        for key in status.all_keys():
            current, fresh = status.cached_status(key)
            if not fresh and key not in self._refreshing:
                # Probed in the background: a slow method must not hold up the rest.
                # Its completion notifies the hub, which then publishes the result.
                task = asyncio.create_task(status._check_safely(key, False))
                task.add_done_callback(lambda _, k=key: self._refreshing.pop(k, None))
                self._refreshing[key] = task
            if current is not None:
                payload = current.to_dict()
                # The retrieval count moves with every job, a probe only once a minute
                payload["retrievals"] = counts.get(current.name.lower(), 0)
                # When it was checked is not a change worth sending on its own
                payload.pop("checked_at", None)
                self._publish(f"status:{key}", {"type": "status", "payload": payload})

    def _publish(self, key: str, message: dict) -> None:
        line = _line(message)
        if self._last.get(key) == line:
            return
        self._last[key] = line
        for queue in self._subscribers:
            queue.put_nowait(line)


def _line(message: dict) -> bytes:
    return (json.dumps(message, default=str, sort_keys=True) + "\n").encode("utf-8")


hub = LiveHub()
