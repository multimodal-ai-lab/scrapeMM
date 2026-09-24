"""`POST /v1/retrieve` -- the endpoint the client's `retrieve()` talks to.

Results stream back as NDJSON, one line per URL, as soon as that URL is done. A batch
of fifty URLs takes minutes, and waiting for the last one before answering would mean
no progress bar, no partial results, and a connection that every reverse proxy in the
path would be entitled to consider dead.

Line kinds:
    {"type": "header",  ...}  once, first: protocol version and the media registry
    {"type": "result",  ...}  once per URL, in completion order
    {"type": "summary", ...}  once, last: counts and duration
    {"type": "error",   ...}  instead of the summary, if the whole batch fell over
"""

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Literal, Optional

import aiohttp
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from scrapemm.common import OutputFormat
from scrapemm.common.paths import APP_NAME
from scrapemm.common.wire import (ContentPayload, PROTOCOL_VERSION, ResponsePayload,
                                  errors_to_wire)
from .. import registry
from ..auth import require_api_key
from ..engine import retrieve_one
from ..jobs import jobs
from ..version import __version__

logger = logging.getLogger(APP_NAME)

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


@router.post("/retrieve")
async def retrieve(request: RetrieveRequest) -> StreamingResponse:
    return StreamingResponse(_stream(request), media_type="application/x-ndjson")


async def _stream(request: RetrieveRequest) -> AsyncIterator[bytes]:
    started = time.time()

    # Duplicates in the batch are retrieved once; the client maps results back onto its
    # own list by URL, exactly as the in-process engine used to.
    urls = list(dict.fromkeys(request.urls))
    methods = _per_url_methods(urls, request.methods)

    job_id = jobs.start(request.model_dump(), len(urls))
    yield _line({
        "type": "header",
        "protocol": PROTOCOL_VERSION,
        "server_version": __version__,
        "job_id": job_id,
        "total": len(urls),
        "registry": registry.info().to_dict(),
    })

    succeeded = failed = 0
    tasks: dict[asyncio.Task, str] = {}
    finished = False
    try:
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
                )): url for url in urls
            }
            for completed in asyncio.as_completed(tasks):
                response = await completed
                payload = _to_payload(response)
                jobs.record(job_id, payload, response.success)
                if response.success:
                    succeeded += 1
                else:
                    failed += 1
                yield _line({"type": "result", "payload": payload.to_dict()})
        jobs.finish(job_id, succeeded, failed)
        finished = True
    except Exception as e:
        logger.error("Retrieval batch failed.", exc_info=True)
        jobs.finish(job_id, succeeded, failed, status="failed")
        finished = True
        yield _line({"type": "error", "message": f"{type(e).__name__}: {e}"})
        return
    finally:
        if not finished:
            # The client went away mid-batch (the stream was closed under us). Nobody is
            # left to receive the rest, so stop scraping it and say what happened --
            # otherwise the job would show as running forever.
            for task in tasks:
                task.cancel()
            jobs.finish(job_id, succeeded, failed, status="interrupted")

    yield _line({
        "type": "summary",
        "job_id": job_id,
        "succeeded": succeeded,
        "failed": failed,
        "duration": time.time() - started,
    })


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
        )
    return ResponsePayload(
        url=response.url,
        content=content,
        method=response.method,
        output_format=response.output_format,
        errors=errors_to_wire(response.errors),
        retrieval_time=response.retrieval_time,
        from_cache=response.from_cache,
    )


def _line(message: dict) -> bytes:
    return (json.dumps(message, default=str) + "\n").encode("utf-8")
