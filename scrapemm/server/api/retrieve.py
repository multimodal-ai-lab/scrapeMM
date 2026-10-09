"""`POST /v1/retrieve` -- the endpoint the client's `retrieve()` talks to.

Results stream back as NDJSON, one line per URL, as soon as that URL is done. A batch
of fifty URLs takes minutes, and waiting for the last one before answering would mean
no progress bar, no partial results, and a connection that every reverse proxy in the
path would be entitled to consider dead.

Line kinds:
    {"type": "header",  ...}  once, first: protocol version and the media registry
    {"type": "result",  ...}  once per URL, in completion order
    {"type": "heartbeat", ...} every HEARTBEAT_INTERVAL seconds without a result: progress
                               counts. Clients ignore it; it exists so that the
                               connection never goes silent
    {"type": "summary", ...}  once, last: counts and duration
    {"type": "error",   ...}  instead of the summary, if the whole batch fell over

A single URL can take minutes (a heavy archive replay, a queue behind the concurrency
limit), and clients -- and proxies in between -- give up on a socket that delivers no
bytes for a while ("Timeout on reading data from socket"). The heartbeat keeps bytes
flowing however long a result takes. Clients that do not know it skip it: every client
dispatches on "type" and ignores what it does not know.
"""

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Literal, Optional

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from scrapemm.common import OutputFormat, ScrapingResponse
from scrapemm.common.exceptions import RetrievalInterrupted
from scrapemm.common.paths import APP_NAME
from scrapemm.common.wire import (ContentPayload, PROTOCOL_VERSION, ResponsePayload,
                                  errors_to_wire)
from .. import interrupts, registry
from ..auth import Principal, require_api_key
from ..engine import retrieve_one
from ..jobs import jobs
from ..version import __version__
from ..workers import run_light

logger = logging.getLogger(APP_NAME)

# Seconds without a result after which a heartbeat line is sent. Well below any sensible
# client read timeout (the scrapeMM client's default is minutes, others use 30-60 s).
HEARTBEAT_INTERVAL = 10.0

router = APIRouter(prefix="/v1", tags=["retrieval"], dependencies=[Depends(require_api_key)])


class RetrieveRequest(BaseModel):
    urls: list[str] = Field(min_length=1)
    actions: Optional[list[dict]] = None
    methods: Any = "auto"
    output_format: OutputFormat = "multimodal"
    max_video_size: Optional[int] = None
    prioritize: Literal["completeness", "speed"] = "completeness"
    use_cache: bool = True
    hedging_delay: Optional[float] = None
    strip: bool = False
    screenshot: bool = False
    enable_archives_fallback: Optional[bool] = None


@router.post("/retrieve")
async def retrieve(request: RetrieveRequest,
                   principal: Principal = Depends(require_api_key)) -> StreamingResponse:
    return StreamingResponse(_stream(request, principal), media_type="application/x-ndjson")


async def _stream(request: RetrieveRequest, principal: Principal,
                  quiet: bool = False) -> AsyncIterator[bytes]:
    """The retrieval as the lines its client reads. `quiet` leaves the (large) results
    out of them, for a job that nobody is reading (see `start_in_background()`)."""
    started = time.time()

    # Duplicates in the batch are retrieved once; the client maps results back onto its
    # own list by URL, exactly as the in-process engine used to.
    urls = list(dict.fromkeys(request.urls))
    methods = _per_url_methods(urls, request.methods)

    # The key the job runs under, by id: its name may change (see `retrieval_stats`)
    params = request.model_dump() | {"api_key": principal.id}
    # Opened by a task of its own, as the write runs in a thread: a client that goes away
    # meanwhile cancels this stream, but the write goes on and the job exists all the same
    # (found as jobs that stayed "running" for good, created in the very moment their
    # client disconnected). So the job is closed below however far this got.
    opening = asyncio.ensure_future(jobs.astart(params, len(urls)))

    job_id = None
    succeeded = failed = 0
    tasks: dict[asyncio.Task, str] = {}
    finished = False
    interrupted_by_user = False

    stopped: set[str] = set()  # URLs a user stopped one by one

    def interrupt() -> None:
        """A user interrupts the job (see `scrapemm.server.interrupts`): its retrievals
        stop, and the stream below closes it and tells the client."""
        nonlocal interrupted_by_user
        interrupted_by_user = True
        for task in tasks:
            task.cancel()

    def interrupt_url(url: str) -> bool:
        """A user stops the retrieval of one URL: it fails as interrupted, and the job
        goes on with the others."""
        for task, task_url in tasks.items():
            if task_url == url and not task.done():
                stopped.add(url)
                task.cancel()
                return True
        return False

    try:
        job_id = await asyncio.shield(opening)
        interrupts.register(job_id, interrupt, interrupt_url)
        yield _line({
            "type": "header",
            "protocol": PROTOCOL_VERSION,
            "server_version": __version__,
            "job_id": job_id,
            "total": len(urls),
            "registry": registry.info().to_dict(),
            "heartbeat": HEARTBEAT_INTERVAL,
        })

        async with aiohttp.ClientSession() as session:
            tasks = {
                asyncio.create_task(retrieve_one(
                    url, session,
                    methods=methods[url],
                    actions=request.actions,
                    output_format=request.output_format,
                    max_video_size=request.max_video_size,
                    prioritize=request.prioritize,
                    use_cache=request.use_cache,
                    hedging_delay=request.hedging_delay,
                    strip=request.strip,
                    screenshot=request.screenshot,
                    enable_archives_fallback=request.enable_archives_fallback,
                )): url for url in urls
            }
            if interrupted_by_user:
                # Interrupted between the job being opened and its retrievals being started
                for task in tasks:
                    task.cancel()
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, timeout=HEARTBEAT_INTERVAL,
                                                   return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    yield _line({"type": "heartbeat", "done": succeeded + failed,
                                 "total": len(urls), "elapsed": round(time.time() - started, 1)})
                    continue
                for completed in done:
                    if completed.cancelled():
                        if tasks[completed] not in stopped:
                            continue  # The whole job was interrupted: nothing came of it
                        response = ScrapingResponse(
                            url=tasks[completed], content=None,
                            output_format=request.output_format,
                            errors={"scrapemm": RetrievalInterrupted("A user stopped this retrieval.")},
                            retrieval_time=time.time() - started)
                    else:
                        response = completed.result()
                    payload = _to_payload(response)
                    await jobs.arecord(job_id, payload, response.success)
                    if response.success:
                        succeeded += 1
                    else:
                        failed += 1
                    if not quiet:
                        # Serialised in a thread: a result carries a whole page, megabytes
                        yield await run_light(_line, {"type": "result", "payload": payload.to_dict()})
        if interrupted_by_user:
            await jobs.afinish(job_id, succeeded, failed, status="interrupted",
                               interrupted_by=interrupts.USER)
            finished = True
            # The client gets what was retrieved so far; the rest it counts as missing
            yield _line({"type": "summary", "job_id": job_id, "succeeded": succeeded,
                         "failed": failed, "duration": time.time() - started,
                         "interrupted_by": interrupts.USER})
            return
        await jobs.afinish(job_id, succeeded, failed)
        finished = True
    except Exception as e:
        logger.error("Retrieval batch failed.", exc_info=True)
        if job_id is not None:
            await jobs.afinish(job_id, succeeded, failed, status="failed")
        finished = True
        yield _line({"type": "error", "message": f"{type(e).__name__}: {e}"})
        return
    finally:
        interrupts.unregister(job_id)
        if not finished:
            # The client went away mid-batch (the stream was closed under us). Nobody is
            # left to receive the rest, so stop scraping it and say what happened --
            # otherwise the job would show as running forever.
            for task in tasks:
                task.cancel()
            by = interrupts.cancelled_by()  # The server, if it is stopping; else the client
            if job_id is not None:
                jobs.finish(job_id, succeeded, failed, status="interrupted", interrupted_by=by)
            else:
                # Gone while the job was still being opened: closed as soon as it is
                opening.add_done_callback(
                    lambda future: interrupts.close_when_opened(jobs, future, by))

    yield _line({
        "type": "summary",
        "job_id": job_id,
        "succeeded": succeeded,
        "failed": failed,
        "duration": time.time() - started,
    })


# The tasks that run jobs nobody is streaming, held so that they are not collected
_background: set[asyncio.Task] = set()


async def start_in_background(request: RetrieveRequest, principal: Principal) -> str:
    """Runs the retrieval as a job of the server's own, with no client reading it, and
    returns the job's id once the job exists. It records its results like any job, and a
    user can interrupt it like any job; if the server stops, it is interrupted by the
    server."""
    stream = _stream(request, principal, quiet=True)
    first = json.loads(await anext(stream))
    if first.get("type") != "header":
        raise RuntimeError(first.get("message", "The job could not be started."))

    async def drain() -> None:
        async for _ in stream:
            pass

    task = asyncio.create_task(drain())
    _background.add(task)
    task.add_done_callback(_background.discard)
    return first["job_id"]


# What a retried URL is retrieved with, of the original job's parameters. The cache is no
# part of it: a retry is a new scrape.
_RETRY_FIELDS = ("actions", "output_format", "max_video_size", "prioritize", "hedging_delay",
                 "strip", "screenshot", "enable_archives_fallback")


class RetryRequest(BaseModel):
    url: str


@router.post("/jobs/{job_id}/retry")
async def retry_url(job_id: str, body: RetryRequest,
                    principal: Principal = Depends(require_api_key)) -> dict:
    """Retrieves a URL of a job again, without the cache and with the job's settings, as a
    new job under the caller's key. Returns the new job's id; the job runs on the server."""
    state = await asyncio.to_thread(jobs.job_state, job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"No job '{job_id}'.")
    params = state["params"]
    urls = params.get("urls") or []
    if body.url not in urls:
        raise HTTPException(status_code=404, detail="That URL is not part of the job.")
    request = RetrieveRequest(
        urls=[body.url], use_cache=False,
        methods=_per_url_methods(list(dict.fromkeys(urls)), params.get("methods", "auto"))[body.url],
        **{field: params[field] for field in _RETRY_FIELDS if field in params})
    return {"job_id": await start_in_background(request, principal), "url": body.url}


def _per_url_methods(urls: list[str], methods: Any) -> dict[str, Any]:
    """Unfolds the `methods` argument into one entry per URL, mirroring the shapes the
    in-process API has always accepted: "auto", a list of method names for every URL,
    or a list of those, one per URL."""
    if methods is None:
        return {url: None for url in urls}
    if isinstance(methods, str):
        return {url: methods for url in urls}
    if isinstance(methods, list) and methods and isinstance(methods[0], str):
        return {url: list(methods) for url in urls}
    if isinstance(methods, list):
        # One entry per URL of the *original* request; duplicates collapsed above take
        # the first occurrence's methods.
        return {url: (methods[i] if i < len(methods) else "auto")
                for i, url in enumerate(urls)}
    return {url: "auto" for url in urls}


def _to_payload(response) -> ResponsePayload:
    content = None
    if response.content is not None:
        multimodal = response.content.multimodal
        content = ContentPayload(
            html=response.content.html,
            markdown=response.content.markdown,
            multimodal=str(multimodal) if multimodal is not None else None,
            items=registry.describe_sequence(multimodal) if multimodal is not None else [],
            stripped=response.content.stripped,
        )
    return ResponsePayload(
        url=response.url,
        content=content,
        method=response.method,
        output_format=response.output_format,
        errors=errors_to_wire(response.errors),
        retrieval_time=response.retrieval_time,
        queue_time=response.queue_time,
        from_cache=response.from_cache,
        screenshot=registry.describe(response.screenshot) if response.screenshot else None,
    )


def _line(message: dict) -> bytes:
    return (json.dumps(message, default=str) + "\n").encode("utf-8")
