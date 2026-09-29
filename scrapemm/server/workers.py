"""Thread pools for work that must stay off the event loop, kept apart by kind.

`asyncio.to_thread` shares the loop's default pool with everything else, and yt-dlp
downloads sit in it for minutes each. Once they fill it, a millisecond-long task -- a
CAPTCHA check, a job-history write, serialising a result -- waits behind them, and a
retrieval that is long done is held back until a thread frees up. Short work therefore
gets a pool of its own.
"""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from scrapemm.common.paths import APP_NAME

logger = logging.getLogger(APP_NAME)

# Short, CPU-bound or local work: page checks and parses, SQLite, JSON
_light = ThreadPoolExecutor(max_workers=8, thread_name_prefix="light")

# A task that waited this long for a thread is logged: the pool is saturated
SLOW_QUEUE = 5.0


async def run_light(function, *args, **kwargs):
    """Runs `function(*args, **kwargs)` in the pool for short work and returns its result."""
    return await run_in(_light, "light", function, *args, **kwargs)


async def run_in(executor: ThreadPoolExecutor, name: str, function, *args, **kwargs):
    """Runs the call in `executor`, logging it when it had to wait long for a thread."""
    submitted = time.monotonic()

    def call():
        if (waited := time.monotonic() - submitted) >= SLOW_QUEUE:
            logger.info(f"The {name} thread pool is saturated: "
                        f"{getattr(function, '__name__', function)} waited {waited:.0f} s for a thread.")
        return function(*args, **kwargs)

    return await asyncio.get_running_loop().run_in_executor(executor, call)
