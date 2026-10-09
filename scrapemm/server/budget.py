"""The time a browser retrieval has left, so that its media give up before it does.

A browser retrieval is cut off after `BROWSER_RETRIEVAL_TIMEOUT` seconds, and a page that
is cut off is lost as a whole. A medium that is slow (a video trickling in through an
archive's replay) used to hold the retrieval until that limit, which failed the page
(perma.cc/75EG-E5GK: ten minutes, then nothing). Everything that waits for a medium is
capped by what is left of the budget instead, so that the page comes back without the
medium rather than not at all.

The budget lives in a context variable, which the retrieval's task and the tasks it
starts inherit: the code that waits needs no extra argument.
"""

import contextvars
import time
from typing import Optional

_deadline: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "retrieval_deadline", default=None)
_abandoned: contextvars.ContextVar[Optional[set]] = contextvars.ContextVar(
    "abandoned_media", default=None)


def start(seconds: float) -> None:
    """Starts the budget of the retrieval that runs in the current context."""
    _deadline.set(time.monotonic() + seconds)
    _abandoned.set(set())


def remaining() -> Optional[float]:
    """Seconds left of the budget, or None if there is none (not a browser retrieval)."""
    deadline = _deadline.get()
    return None if deadline is None else deadline - time.monotonic()


def cap(seconds: float, reserve: float = 10.0, floor: float = 0.0) -> float:
    """`seconds`, or less if the budget runs out sooner: it leaves `reserve` seconds for
    what follows the wait. Once that time is used up, 0 (or `floor`): the caller gives up
    at once. Granting every wait a few seconds more instead let a page's media, queued two
    at a time per host, wait on in rounds well past the budget's end -- and the page that
    the budget was to save was cut off and lost as a whole (kallxo.com, 30+ images)."""
    left = remaining()
    if left is None:
        return seconds
    return max(min(seconds, left - reserve), floor)


def abandoned() -> set:
    """The media this retrieval has given up on (their addresses): not waited for again
    when a later step comes across them."""
    found = _abandoned.get()
    return found if found is not None else set()
