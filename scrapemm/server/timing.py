"""When a retrieval's time starts.

A URL's `retrieval_time` runs from its first outgoing request to its result, and
`queue_time` from its arrival until that first request. Under load a URL first waits
for the server's concurrency slot and then, if the browser is the first thing it tries,
for a browser slot (`_BrowserSlot`): neither is the target's or the method's doing, so
neither counts as retrieval time.

The first request is marked (see `work_started()`) where a URL's work begins: the host
reachability check, the direct download of a media file, a non-browser method or
integration starting, a request on the retrieval's HTTP session, and a browser page
once it has its slot. Whatever comes first starts the clock; later marks change
nothing. A URL that never sends anything (cache hit, refused domain) starts as soon as
it is admitted."""

import time
from contextvars import ContextVar
from typing import Optional

import aiohttp


class RetrievalClock:
    def __init__(self):
        self.entered = time.time()
        self.admitted: Optional[float] = None
        self.started: Optional[float] = None

    def admit(self) -> None:
        if self.admitted is None:
            self.admitted = time.time()

    def mark(self) -> None:
        if self.started is None:
            self.started = time.time()

    def stamp(self, response):
        """Sets the response's retrieval and queue times by this clock. In place: the
        response may be the one just put in the cache, which should agree with it."""
        start = self.started or self.admitted or self.entered
        response.retrieval_time = time.time() - start
        response.queue_time = start - self.entered
        return response


_current: ContextVar[Optional[RetrievalClock]] = ContextVar("retrieval_clock", default=None)


def current() -> Optional[RetrievalClock]:
    return _current.get()


def start_clock() -> RetrievalClock:
    """A new clock for the retrieval running in this context (and the tasks it spawns)."""
    clock = RetrievalClock()
    _current.set(clock)
    return clock


def work_started() -> None:
    """Marks the current retrieval's first outgoing request, if not marked yet."""
    if (clock := _current.get()) is not None:
        clock.mark()


async def _on_request_start(session, context, params) -> None:
    work_started()


def request_trace() -> aiohttp.TraceConfig:
    """Marks the start on the first request of an HTTP session (see module docstring)."""
    trace = aiohttp.TraceConfig()
    trace.on_request_start.append(_on_request_start)
    return trace
