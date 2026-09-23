"""Archive.today: its backlog, its caches, and the CAPTCHA panel.

The working rhythm this supports is the one the integration was built around. Gated
snapshots do not block a batch; they go into a buffer. When somebody has a minute, they
open the panel, solve one check, and the whole buffer is retrieved and cached inside the
five minutes the session lasts.
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from scrapemm.common.paths import APP_NAME
from ..auth import require_api_key
from ..captcha_session import DEFAULT_TIMEOUT, session
from ..secrets import is_set

logger = logging.getLogger(APP_NAME)

router = APIRouter(prefix="/v1/archive-today", tags=["archive.today"],
                   dependencies=[Depends(require_api_key)])


class SessionRequest(BaseModel):
    timeout: float = DEFAULT_TIMEOUT


@router.get("")
async def state() -> dict:
    from ..integrations.archive_today import (count_cached_archive_today_pages,
                                              get_archive_today_buffer)
    return {
        "buffer": get_archive_today_buffer(),
        "cached_pages": count_cached_archive_today_pages(),
        # Whether cookies are stored at all. They expire after roughly five minutes,
        # so this says "a session was established at some point", not "usable now".
        "has_stored_session": is_set("archive_today_cookie"),
        "captcha": session.status(),
    }


@router.post("/session")
async def start_session(body: SessionRequest) -> dict:
    return await session.start(timeout=body.timeout)


@router.get("/session")
async def session_status() -> dict:
    return session.status()


@router.delete("/session")
async def cancel_session() -> dict:
    return await session.cancel()


@router.post("/drain")
async def drain() -> dict:
    """Retrieves the backlog with the session that is already stored, without asking
    for a new CAPTCHA. Useful only while a session is still valid."""
    from ..integrations.archive_today import retrieve_buffered_archive_today
    cached = await retrieve_buffered_archive_today()
    return {"cached": cached}


@router.delete("/buffer")
async def clear_buffer() -> dict:
    from ..integrations.archive_today import (clear_archive_today_buffer,
                                              get_archive_today_buffer)
    dropped = len(get_archive_today_buffer())
    clear_archive_today_buffer()
    return {"dropped": dropped}
