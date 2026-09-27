"""CAPTCHA challenges: what is waiting for a human, and the two decisions on each.

The working rhythm this supports is the one Archive.today was built around, now for
every site. A gated URL does not block its batch; it is queued with a challenge for its
domain. When somebody has a minute, they open the CAPTCHA page and, per challenge,
either solve the check in the server's browser -- after which the queue is retrieved and
cached -- or discard it.
"""

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import challenges
from ..auth import require_api_key
from ..captcha_session import DEFAULT_TIMEOUT, session

router = APIRouter(prefix="/v1/captcha", tags=["captcha"],
                   dependencies=[Depends(require_api_key)])


class SolveRequest(BaseModel):
    timeout: float = DEFAULT_TIMEOUT


@router.get("")
async def overview() -> dict:
    from ..integrations.archive_today import count_cached_archive_today_pages
    return {
        "challenges": challenges.list_challenges(),
        "session": session.status(),
        # Without a display there is nothing for the human to look at
        "solvable": bool(os.getenv("DISPLAY")) or os.name == "nt",
        "archive_today_cached_pages": count_cached_archive_today_pages(),
    }


# Before the /{domain} routes, which would otherwise take "session" for a domain
@router.get("/session")
async def session_status() -> dict:
    return session.status()


@router.delete("/session")
async def cancel_session() -> dict:
    return await session.cancel()


@router.post("/session/no-captcha")
async def report_no_captcha() -> dict:
    """The page in the panel shows no check, only the normal content: logs the case as
    a possible false detection and retrieves the queue."""
    try:
        return session.report_no_captcha()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/{domain}/solve")
async def solve(domain: str, body: SolveRequest) -> dict:
    _require(domain)
    return await session.start(domain, timeout=body.timeout)


@router.post("/{domain}/retry")
async def retry(domain: str) -> dict:
    """Retrieves the queue without asking for a new check -- worth a try when the
    clearance from an earlier solve may still be valid."""
    _require(domain)
    if session.running:
        raise HTTPException(status_code=409, detail="A CAPTCHA session is under way.")
    retrieved, remaining = await challenges.drain(domain)
    return {"retrieved": retrieved, "remaining": remaining}


@router.delete("/{domain}")
async def discard(domain: str) -> dict:
    _require(domain)
    if session.running and session.domain == domain:
        await session.cancel()
    return {"dropped": challenges.discard(domain)}


def _require(domain: str) -> None:
    if not any(c["domain"] == domain for c in challenges.list_challenges()):
        raise HTTPException(status_code=404,
                            detail=f"There is no open CAPTCHA challenge for {domain}.")
