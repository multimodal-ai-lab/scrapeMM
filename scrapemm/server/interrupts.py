"""Why a job was interrupted, and how a user interrupts one.

A job that does not finish is "interrupted", by one of three parties:

* the user, who pressed the button in the web UI (or called `POST /v1/jobs/{id}/interrupt`),
* the client, which disconnected while its job was running,
* the server, which stopped or restarted: also what a job found still running at startup
  was interrupted by, as nothing can be working on it.

Jobs that run on this server register how they are cancelled, so that a user can.
"""

import asyncio
import logging
import signal
from typing import Callable, Optional

logger = logging.getLogger("scrapeMM")

USER, CLIENT, SERVER = "user", "client", "server"
CAUSES = (USER, CLIENT, SERVER)

_cancellers: dict[str, Callable[[], None]] = {}
_shutting_down = False


def register(job_id: str, cancel: Callable[[], None]) -> None:
    """Makes the job interruptible by a user: `cancel` stops its work, and the job's own
    code closes it as interrupted (by the user) from there."""
    _cancellers[job_id] = cancel


def unregister(job_id: Optional[str]) -> None:
    _cancellers.pop(job_id, None)


def interrupt(job_id: str) -> bool:
    """Interrupts the job on behalf of a user. False if no job of that id is being worked
    on by this server."""
    cancel = _cancellers.get(job_id)
    if cancel is None:
        return False
    cancel()
    return True


def begin_shutdown() -> None:
    """The server is stopping: what is cancelled from here on is cancelled by it."""
    global _shutting_down
    _shutting_down = True


def watch_shutdown_signals() -> None:
    """Flags the shutdown as soon as the server is told to stop. Uvicorn cancels the open
    streams before it runs the application's shutdown code, which is too late to tell
    their jobs the server stopped, so the signal handlers it installed are wrapped."""
    for name in ("SIGTERM", "SIGINT"):
        number = getattr(signal, name, None)
        try:
            previous = signal.getsignal(number)

            def handler(signum, frame, previous=previous):
                begin_shutdown()
                if callable(previous):
                    previous(signum, frame)

            signal.signal(number, handler)
        except (ValueError, OSError, TypeError):
            pass  # Not the main thread, or no such signal here: the shutdown is flagged late


def cancelled_by() -> str:
    """Who cancelled work that nobody asked to cancel: the server if it is shutting down,
    else the client that went away."""
    return SERVER if _shutting_down else CLIENT


def close_when_opened(store, opening: "asyncio.Future", by: str) -> None:
    """Closes as interrupted a job whose opening was still under way when its requester
    went away. The write runs in a worker thread and outlives the cancelled request, so
    the job exists all the same and nobody else would ever close it. Meant as a done
    callback of the future opening the job."""
    if not opening.cancelled() and opening.exception() is None:
        store.finish(opening.result(), 0, 0, status="interrupted", interrupted_by=by)
