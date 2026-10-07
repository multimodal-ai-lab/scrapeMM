"""Driving a CAPTCHA challenge from the web UI, for any site.

A solved check is bound to the browser that solved it, often to its IP address too, and
many only last minutes. So the human cannot solve it in their own browser and hand over
a cookie: they have to operate *the server's* browser.

That is what the CAPTCHA panel does. The server opens the challenge page in its headed
Chromium on the virtual display, x11vnc exposes that display, and the UI shows it over a
WebSocket (see `api/vnc.py`). The moment the check passes, the session ends -- the panel
closes -- and the URLs queued with the challenge are retrieved in the background while
the clearance is fresh (see `challenges.py`).

Only one session can run at a time -- there is only one browser, and one display to
show it on. Retrievals of queues run alongside, one per domain.
"""

import asyncio
import logging
import time
from typing import Optional

from scrapemm.common.paths import APP_NAME
from . import challenges

logger = logging.getLogger(APP_NAME)

DEFAULT_TIMEOUT = 300.0

# How long reporting "no CAPTCHA" waits for the session to wrap up, so that the answer
# already says it has ended and the panel closes
REPORT_SETTLE = 3.0


class CaptchaSession:
    """The one CAPTCHA-solving session this server may have in flight."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self.state: str = "idle"  # idle | running | passed | failed | cancelled
        self.domain: Optional[str] = None
        self.message: str = ""
        self.started_at: Optional[float] = None
        self.deadline: Optional[float] = None
        # Set when the human reports that the page shows no check at all
        self._no_captcha = asyncio.Event()
        # Queues being retrieved after a session, per domain
        self._drains: dict[str, asyncio.Task] = {}

    @property
    def running(self) -> bool:
        """Whether a human is at work, i.e. whether the panel shows the browser."""
        return self._task is not None and not self._task.done()

    def retrieving(self, domain: str) -> bool:
        task = self._drains.get(domain)
        return task is not None and not task.done()

    async def start(self, domain: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
        async with self._lock:
            if self.running or self.retrieving(domain):
                return self.status()

            self.state = "running"
            self.domain = domain
            self.message = (f"Opening {domain} in the server's browser. Solve the check in "
                            f"the panel.")
            self.started_at = time.time()
            self.deadline = self.started_at + timeout
            self._no_captcha = asyncio.Event()
            self._task = asyncio.create_task(self._run(domain, timeout))
            return self.status()

    async def report_no_captcha(self) -> dict:
        """The human sees the normal page, no check: the detection was presumably
        wrong. Recorded for debugging, then the queue is retrieved without it.

        Works during a session, and also right after one that "passed" but left the
        queue waiting: the page shown for solving may be fine while the content
        retrieved for the queue is what the detection keeps flagging."""
        if not self._can_report_no_captcha():
            raise RuntimeError("There is nothing to report: no session is under way, and "
                               "the last one left no queue behind.")
        if self.running:
            self._no_captcha.set()
            self.message = ("Recorded that no CAPTCHA was shown. Retrieving the queued "
                            "URLs in the background, without the CAPTCHA detection…")
            # The session ends within moments; answering after that closes the panel
            await asyncio.wait({self._task}, timeout=REPORT_SETTLE)
            return self.status()

        self.state = "passed"
        self.message = ("Recorded that no CAPTCHA was shown. Retrieving the queued URLs "
                        "in the background, without the CAPTCHA detection…")
        self._start_drain(self.domain, self._drain_confirmed(self.domain))
        return self.status()

    def _can_report_no_captcha(self) -> bool:
        # Archive.today's session has its own detection, which this cannot override
        if not self.domain or self.domain == challenges.ARCHIVE_TODAY:
            return False
        if self.running:
            return not self._no_captcha.is_set()
        if self.retrieving(self.domain):
            return False
        return self.state in ("passed", "failed") and challenges.store.get(self.domain) is not None

    async def _run(self, domain: str, timeout: float) -> None:
        """The human's part. Ends as soon as the check is passed: the queue is then
        retrieved by a task of its own, so the panel does not wait for it."""
        try:
            passed, reported = await challenges.await_solution(
                domain, timeout, no_captcha=self._no_captcha)
        except asyncio.CancelledError:
            self.state = "cancelled"
            self.message = "The session was cancelled."
            raise
        except Exception as e:
            logger.warning(f"The CAPTCHA session for {domain} failed.", exc_info=True)
            self.state = "failed"
            self.message = f"{type(e).__name__}: {e}"
            return

        if not passed:
            self.state = "failed"
            self.message = ("The check was not passed in time. Nothing was retrieved; "
                            "start another session to try again.")
            return
        self.state = "passed"
        self.message = ("No CAPTCHA was shown; logged as a possible false detection. "
                        if reported else "Passed. ")
        self.message += "Retrieving the queued URLs in the background…"
        # A report that came in after the check had passed was not applied; clearing it
        # lets the human report again, now against the content that was flagged
        self._no_captcha = asyncio.Event()
        self._start_drain(domain, self._drain(domain, reported))

    def _start_drain(self, domain: str, coroutine) -> None:
        self._drains[domain] = task = asyncio.create_task(coroutine)
        task.add_done_callback(
            lambda t: self._drains.pop(domain, None) if self._drains.get(domain) is t else None)

    def _tell(self, domain: str, message: str) -> None:
        """Reports how a queue's retrieval went, unless a newer session took the stage."""
        logger.info(f"CAPTCHA queue of {domain}: {message}")
        if self.domain == domain and not self.running:
            self.message = message

    async def _drain(self, domain: str, reported: bool) -> None:
        try:
            retrieved, remaining = await challenges.drain(domain, trust_content=reported)
        except Exception as e:
            logger.warning(f"Retrieving the {domain} queue failed.", exc_info=True)
            self._tell(domain, f"Passed, but retrieving the queue failed: "
                               f"{type(e).__name__}: {e}")
            return
        message = ("No CAPTCHA was shown; logged as a possible false detection. "
                   if reported else "Passed. ")
        message += f"{retrieved} queued URL(s) were retrieved and cached."
        if remaining:
            message += (f" {remaining} still wait: the site gated again, so solve it "
                        f"once more to continue.")
            if not reported:
                message += (" If the page shows no check, report that with "
                            "\"No CAPTCHA here\".")
        self._tell(domain, message)

    async def _drain_confirmed(self, domain: str) -> None:
        try:
            retrieved, remaining = await challenges.confirm_no_captcha(domain)
        except Exception as e:
            logger.warning(f"Retrieving the {domain} queue without detection failed.",
                           exc_info=True)
            self._tell(domain, f"{type(e).__name__}: {e}")
            return
        message = (f"Logged as a possible false detection. {retrieved} queued URL(s) "
                   f"were retrieved and cached.")
        if remaining:
            message += f" {remaining} could not be retrieved and still wait."
        self._tell(domain, message)

    async def stop_retrieving(self, domain: str) -> None:
        task = self._drains.get(domain)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def cancel(self) -> dict:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self.state == "running":
            self.state = "cancelled"
            self.message = "The session was cancelled."
        return self.status()

    def status(self) -> dict:
        remaining = None
        if self.running and self.deadline is not None:
            remaining = max(0.0, self.deadline - time.time())
        return {
            "state": self.state,
            "running": self.running,
            "domain": self.domain,
            "message": self.message,
            "started_at": self.started_at,
            "seconds_remaining": remaining,
            "can_report_no_captcha": self._can_report_no_captcha(),
            # Domains whose queue is being retrieved in the background
            "retrieving": sorted(d for d in self._drains if self.retrieving(d)),
        }


session = CaptchaSession()
