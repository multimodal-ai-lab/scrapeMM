"""Driving the Archive.today access check from the web UI.

Archive.today binds a solved check to the browser that solved it and to that browser's
IP address, and the session lasts about five minutes. So the human cannot solve it in
their own browser and hand over a cookie: they have to operate *the server's* browser.

That is what the CAPTCHA panel does. The server opens the snapshot in its headed
Chromium on the virtual display, x11vnc exposes that display, and the UI shows it over
a WebSocket (see `api/vnc.py`). The moment the check passes, the existing machinery
takes over: the session cookies are stored and the buffered backlog is retrieved within
those five minutes.

Only one session can run at a time -- there is only one browser, and it holds an
exclusive lock on its profile.
"""

import asyncio
import logging
import time
from typing import Optional

from scrapemm.common.paths import APP_NAME

logger = logging.getLogger(APP_NAME)

DEFAULT_TIMEOUT = 300.0


class CaptchaSession:
    """The one CAPTCHA-solving session this server may have in flight."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self.state: str = "idle"  # idle | running | passed | failed | cancelled
        self.message: str = ""
        self.started_at: Optional[float] = None
        self.deadline: Optional[float] = None
        self.drained: Optional[int] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, timeout: float = DEFAULT_TIMEOUT) -> dict:
        async with self._lock:
            if self.running:
                return self.status()

            self.state = "running"
            self.message = ("Opening Archive.today in the server's browser. Solve the "
                            "check in the panel.")
            self.started_at = time.time()
            self.deadline = self.started_at + timeout
            self.drained = None
            self._task = asyncio.create_task(self._run(timeout))
            return self.status()

    async def _run(self, timeout: float) -> None:
        from .integrations import NAME_TO_INTEGRATION

        integration = NAME_TO_INTEGRATION["archive.today"]
        try:
            passed = await integration.capture_session(timeout=timeout)
        except asyncio.CancelledError:
            self.state = "cancelled"
            self.message = "The session was cancelled."
            raise
        except Exception as e:
            logger.warning("Archive.today CAPTCHA session failed.", exc_info=True)
            self.state = "failed"
            self.message = f"{type(e).__name__}: {e}"
            return

        if passed:
            self.state = "passed"
            self.message = ("Session established. The buffered snapshots were retrieved "
                            "and cached.")
        else:
            self.state = "failed"
            self.message = ("The check was not passed in time. Nothing was stored; start "
                            "another session to try again.")

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
            "message": self.message,
            "started_at": self.started_at,
            "seconds_remaining": remaining,
        }


session = CaptchaSession()
