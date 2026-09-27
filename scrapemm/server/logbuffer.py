"""The server's recent log, kept in memory for the web UI's Logs page.

The container's own log stream (`docker compose logs`) is out of reach from inside the
container, so the records are captured where they are made: a logging handler on
scrapeMM's logger, uvicorn's and the root logger (for everything the libraries log).
Output that bypasses Python's logging -- the entrypoint's startup lines, Chromium's and
FFmpeg's own prints -- only appears in `docker compose logs`.

Bounded twice: the backlog keeps the latest records only, and a viewer that cannot keep
up loses lines rather than growing the server's memory.
"""

import asyncio
import itertools
import logging
import threading
import time
from collections import deque
from typing import AsyncIterator

from scrapemm.common.paths import APP_NAME

MAX_RECORDS = 5000
MAX_QUEUED_PER_VIEWER = 2000
HEARTBEAT = 15.0  # Seconds of silence before a keep-alive line, see live.py

# uvicorn's loggers do not propagate to the root, and neither does scrapeMM's
CAPTURED_LOGGERS = (APP_NAME, "uvicorn", "uvicorn.access", "")


class LogBuffer(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self._records: deque[dict] = deque(maxlen=MAX_RECORDS)
        self._sequence = itertools.count(1)
        self._viewers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
        self._viewers_lock = threading.Lock()
        self._traceback_formatter = logging.Formatter()

    def emit(self, record: logging.LogRecord) -> None:
        # Runs on whichever thread logged, so viewers are notified through their loop
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + self._traceback_formatter.formatException(record.exc_info)
            entry = {"seq": next(self._sequence), "time": record.created,
                     "level": record.levelname, "logger": record.name or "root",
                     "message": message}
        except Exception:
            return  # Logging must never be the reason something fails
        with self._viewers_lock:
            self._records.append(entry)
            viewers = list(self._viewers)
        for loop, queue in viewers:
            try:
                loop.call_soon_threadsafe(_offer, queue, entry)
            except RuntimeError:
                pass  # That viewer's loop is gone

    async def follow(self) -> AsyncIterator[dict]:
        """The backlog, then every new record as it is logged, with a ping during
        silence. Ends when the consumer stops iterating."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUED_PER_VIEWER)
        viewer = (asyncio.get_running_loop(), queue)
        with self._viewers_lock:
            backlog = list(self._records)
            self._viewers.add(viewer)
        try:
            yield {"type": "backlog", "records": backlog, "capacity": MAX_RECORDS}
            while True:
                try:
                    yield {"type": "record", "record": await asyncio.wait_for(queue.get(), HEARTBEAT)}
                except asyncio.TimeoutError:
                    yield {"type": "ping", "at": time.time()}
        finally:
            with self._viewers_lock:
                self._viewers.discard(viewer)


def _offer(queue: asyncio.Queue, entry: dict) -> None:
    try:
        queue.put_nowait(entry)
    except asyncio.QueueFull:
        pass  # A stalled viewer loses lines; the backlog on reconnect has them


buffer = LogBuffer()


def install() -> None:
    """Attaches the buffer to every logger worth showing. Call after uvicorn configured
    its logging, which replaces handlers set before."""
    for name in CAPTURED_LOGGERS:
        target = logging.getLogger(name)
        if buffer not in target.handlers:
            target.addHandler(buffer)
