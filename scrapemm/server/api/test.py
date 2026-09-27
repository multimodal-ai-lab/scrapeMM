"""The Test page: the URL suite, running it, and the reports of past runs."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import testsuite
from ..auth import require_api_key

router = APIRouter(prefix="/v1/test", tags=["test"], dependencies=[Depends(require_api_key)])


class SuiteEntry(BaseModel):
    url: str
    category: str = "Added"
    expected: dict[str, int] = {}


@router.get("")
async def overview() -> dict:
    """The suite, the current (or last) run and the kept history, in one answer."""
    return {"suite": testsuite.suite(), "run": testsuite.run.status(),
            "history": testsuite.history()}


@router.get("/run")
async def run_status() -> dict:
    """Polled while a run is under way: its live statistics."""
    return testsuite.run.status()


@router.post("/run")
async def start_run() -> dict:
    try:
        return testsuite.run.start()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/run")
async def cancel_run() -> dict:
    return await testsuite.run.cancel()


@router.post("/run/rerun-captchas")
async def rerun_captchas() -> dict:
    """Retrieves again the latest run's URLs that ran into a CAPTCHA, cache allowed."""
    try:
        return testsuite.run.rerun_captchas()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/runs/{run_id}")
async def past_run(run_id: str) -> dict:
    report = testsuite.past_run(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"No test run '{run_id}'.")
    return report


@router.post("/suite")
async def add_url(entry: SuiteEntry) -> dict:
    try:
        return testsuite.add(entry.url, entry.category, entry.expected)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/suite")
async def remove_url(url: str = Query(...)) -> dict:
    if not testsuite.remove(url):
        raise HTTPException(status_code=404, detail="That URL is not in the suite.")
    return {"removed": url}


@router.post("/suite/restore")
async def restore_defaults() -> dict:
    return {"restored": testsuite.restore_defaults()}
