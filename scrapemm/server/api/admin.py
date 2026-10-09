"""Everything the web UI needs besides retrieval: status, secrets, configuration,
the blacklist, the cache, the job history and the media files.

Secrets are write-only here. `GET /v1/secrets` says which ones are set; it never says
what they are, and neither do the logs.
"""

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from scrapemm.common.paths import APP_NAME
from .. import interrupts, registry, status as status_module
from ..auth import Principal, require_admin, require_api_key
from ..blacklist import blacklist
from ..cache import cache, immutable, KINDS
from ..config import SETTINGS, get_config, update_config
from ..jobs import SORTS, jobs
from ..toggles import set_enabled
from ..secrets import (MANAGED_SECRETS, SECRETS, describe_secrets, remove_secret,
                       rotate_key, set_secret)
from ..version import __version__

logger = logging.getLogger(APP_NAME)

router = APIRouter(prefix="/v1", tags=["admin"], dependencies=[Depends(require_api_key)])
ADMIN = [Depends(require_admin)]  # For what configures the server or reveals its credentials


# --- Version and status -----------------------------------------------------------

@router.get("/version")
async def version() -> dict:
    from scrapemm.common.wire import PROTOCOL_VERSION
    return {"version": __version__, "protocol": PROTOCOL_VERSION}


@router.get("/integrations")
async def integrations(force: bool = Query(default=False)) -> dict:
    statuses = await status_module.check_all(force=force)
    return {"integrations": [s.to_dict() for s in statuses]}


@router.get("/integrations/stream")
async def integrations_stream(force: bool = Query(default=False)) -> StreamingResponse:
    """The same statuses, but as NDJSON, one per line as each probe finishes.

    Probing seventeen methods takes as long as the slowest of them -- several seconds on
    a cold cache. Waiting for all of them before showing any leaves the dashboard blank
    for that whole time; streaming lets each card resolve the moment its own probe does.

    The first line carries every method's key *and* display name, so the UI can draw the
    cards with their real names immediately rather than filling them in later.
    """
    async def lines():
        methods = status_module.all_methods()
        keys = [m["key"] for m in methods]
        yield _ndjson({"type": "header", "methods": methods, "total": len(methods)})

        async def probe(key: str):
            try:
                return await status_module.check(key, force=force)
            except Exception as e:
                logger.debug(f"Streaming probe of {key} failed.", exc_info=True)
                return status_module.IntegrationStatus(
                    name=key, key=key, connected=False,
                    detail=f"{type(e).__name__}: {e}")

        tasks = [asyncio.create_task(probe(key)) for key in keys]
        try:
            for completed in asyncio.as_completed(tasks):
                result = await completed
                yield _ndjson({"type": "status", "payload": result.to_dict()})
        finally:
            for task in tasks:
                task.cancel()
        yield _ndjson({"type": "done"})

    return StreamingResponse(lines(), media_type="application/x-ndjson")


def _ndjson(message: dict) -> bytes:
    return (json.dumps(message, default=str) + "\n").encode("utf-8")


@router.post("/integrations/{name}/check")
async def check_integration(name: str) -> dict:
    try:
        return (await status_module.check(name, force=True)).to_dict()
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


class EnabledFlag(BaseModel):
    enabled: bool


@router.put("/integrations/{name}/enabled", dependencies=ADMIN)
async def set_integration_enabled(name: str, body: EnabledFlag) -> dict:
    """Switches a retrieval method on or off. A disabled one is dropped from the method
    list before retrieval, so it costs nothing rather than failing its way down it."""
    if name.lower() not in status_module.all_keys():
        raise HTTPException(status_code=404,
                            detail=f"Unknown integration or method '{name}'.")
    set_enabled(name, body.enabled)
    status_module.invalidate(name)
    return (await status_module.check(name, force=True)).to_dict()


@router.get("/environment")
async def environment() -> dict:
    return await status_module.environment()


@router.get("/live")
async def live() -> StreamingResponse:
    """The dashboard's live view as NDJSON: a header with every method, then the
    environment and each method's status whenever one of them changes, and a ping
    every few seconds of silence. See `live.py` for how it is kept cheap."""
    from ..live import hub
    return StreamingResponse(hub.subscribe(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/logs/stream", dependencies=ADMIN)
async def logs_stream() -> StreamingResponse:
    """The server's log as NDJSON: the recent backlog first, then every new record as
    it is logged, and a ping every few seconds of silence. See `logbuffer.py`."""
    from ..logbuffer import buffer

    async def lines():
        async for message in buffer.follow():
            yield _ndjson(message)

    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# --- Secrets ----------------------------------------------------------------------

class SecretValue(BaseModel):
    value: str


@router.get("/secrets", dependencies=ADMIN)
async def list_secrets() -> dict:
    return {"secrets": describe_secrets()}


@router.put("/secrets/{name}", dependencies=ADMIN)
async def put_secret(name: str, body: SecretValue) -> dict:
    if name not in SECRETS:
        raise HTTPException(status_code=404, detail=f"Unknown secret '{name}'.")
    if name in MANAGED_SECRETS:
        raise HTTPException(
            status_code=400,
            detail=f"'{name}' is maintained by the server itself and cannot be set here.")
    if not body.value.strip():
        raise HTTPException(status_code=400, detail="The value must not be empty.")

    set_secret(name, body.value.strip())
    _refresh_dashboard(name)
    logger.info(f"Secret '{name}' was set through the API.")
    return {"name": name, "is_set": True}


@router.delete("/secrets/{name}", dependencies=ADMIN)
async def delete_secret(name: str) -> dict:
    if name not in SECRETS:
        raise HTTPException(status_code=404, detail=f"Unknown secret '{name}'.")
    removed = remove_secret(name)
    _refresh_dashboard(name)
    return {"name": name, "removed": removed, "is_set": False}


def _refresh_dashboard(secret_name: str) -> None:
    """Re-probes the cards a changed secret affects, so the dashboard shows the effect
    at once rather than after the status TTL lapses. A secret no card uses, like a
    search provider's key, affects nothing -- and must not reach `invalidate()` with no
    names, which means "everything" and would re-probe every integration."""
    if affected := status_module.secrets_to_integrations(secret_name):
        status_module.invalidate(*affected)


@router.post("/secrets/rotate-key", dependencies=ADMIN)
async def rotate_secrets_key() -> dict:
    try:
        rotate_key()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"rotated": True}


# --- Configuration ----------------------------------------------------------------

@router.get("/config", dependencies=ADMIN)
async def read_config() -> dict:
    return {"config": get_config(),
            "settings": {name: kind.__name__ for name, kind in SETTINGS.items()}}


@router.patch("/config", dependencies=ADMIN)
async def patch_config(body: dict[str, Any]) -> dict:
    unknown = [name for name in body if name not in SETTINGS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown setting(s): {', '.join(unknown)}. Allowed: "
                   f"{', '.join(sorted(SETTINGS))}.")

    coerced = {}
    for name, value in body.items():
        expected = SETTINGS[name]
        if value is None:
            coerced[name] = None
            continue
        try:
            coerced[name] = value if isinstance(value, expected) else expected(value)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"'{name}' expects {expected.__name__}, got {value!r}.")

    update_config(**coerced)
    if {"job_retention_days", "max_jobs"} & coerced.keys():
        await asyncio.to_thread(jobs.prune)  # A lower limit applies at once
    return {"config": get_config()}


# --- Blacklist --------------------------------------------------------------------

class BlacklistEntry(BaseModel):
    reason: str = "Blacklisted manually."


@router.get("/blacklist")
async def read_blacklist() -> dict:
    return {"domains": blacklist.domains()}


@router.post("/blacklist/{domain}")
async def add_to_blacklist(domain: str, body: BlacklistEntry) -> dict:
    # Manual entries are permanent: they express a decision, not an observation that
    # may go stale, so they do not expire the way CAPTCHA-triggered ones do.
    blacklist.add(domain, body.reason, permanent=True)
    return {"domain": domain, "reason": body.reason}


@router.delete("/blacklist/{domain}")
async def remove_from_blacklist(domain: str) -> dict:
    return {"domain": domain, "removed": blacklist.remove(domain)}


# --- Cache ------------------------------------------------------------------------

@router.get("/cache", dependencies=ADMIN)
async def read_cache() -> dict:
    """Live figures and the settings: entries, size_mb, enabled, ttl, max_entries, max_mb."""
    return cache.stats()


class CacheConfig(BaseModel):
    enabled: Optional[bool] = None
    ttl: Optional[float] = None  # Seconds an entry lives; 0 disables the cache
    max_entries: Optional[int] = None
    max_mb: Optional[float] = None  # Text held in memory, in MB
    immutable_max_mb: Optional[float] = None  # The permanent tier on disk, in MB


@router.put("/cache/config", dependencies=ADMIN)
async def configure_cache(body: CacheConfig) -> dict:
    """Changes the cache's settings; they apply at once, with no restart."""
    if body.ttl is not None and body.ttl < 0:
        raise HTTPException(status_code=400, detail="The lifetime cannot be negative.")
    if body.max_entries is not None and not 1 <= body.max_entries <= 1_000_000:
        raise HTTPException(status_code=400, detail="Max entries must be between 1 and 1,000,000.")
    if body.max_mb is not None and not 1 <= body.max_mb <= 64 * 1024:
        raise HTTPException(status_code=400, detail="Max size must be between 1 MB and 64 GB.")
    if body.immutable_max_mb is not None and not 1 <= body.immutable_max_mb <= 1024 * 1024:
        raise HTTPException(status_code=400,
                            detail="The permanent cache's size must be between 1 MB and 1 TB.")
    changes = {name: value for name, value in (
        ("cache_enabled", body.enabled), ("cache_ttl", body.ttl),
        ("cache_max_entries", body.max_entries), ("cache_max_mb", body.max_mb),
        ("cache_immutable_max_mb", body.immutable_max_mb))
        if value is not None}
    update_config(**changes)  # Applies them to the cache, see `config._apply_config()`
    return cache.stats()


@router.post("/cache/clear", dependencies=ADMIN)
async def clear_the_cache(tier: str = Query(default="recent", pattern="^(recent|all)$")) -> dict:
    """Empties the recent tier; with `tier=all`, the permanent one too -- which holds
    Archive.today pages that cannot be retrieved again without solving its CAPTCHA."""
    entries = len(cache)
    cache.clear()
    permanent = await asyncio.to_thread(immutable.clear, KINDS) if tier == "all" else 0
    return {"cleared": entries, "cleared_permanent": permanent}


# --- Job history ------------------------------------------------------------------

@router.get("/jobs")
async def list_jobs(
        limit: int = Query(default=25, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        job_status: Optional[str] = Query(default=None, alias="status"),
        url: Optional[str] = Query(default=None,
                                   description="Substring match on any URL of the job"),
        output_format: Optional[str] = Query(default=None),
        method: Optional[str] = Query(default=None),
        success: Optional[bool] = Query(
            default=None,
            description="True keeps jobs with at least one success, False with at "
                        "least one failure"),
        outcome: Optional[str] = Query(
            default=None,
            description="Keeps jobs with at least one result of this outcome: ok, "
                        "unavailable or error, or a kind of unavailability (missing, "
                        "paywall, captcha, blocked, rate_limit, unsupported). See "
                        "scrapemm.common.outcome."),
        since: Optional[float] = Query(default=None,
                                       description="UNIX timestamp, inclusive"),
        until: Optional[float] = Query(default=None,
                                       description="UNIX timestamp, inclusive"),
        sort: str = Query(default="newest",
                          description="newest, oldest, longest, shortest, most_urls, "
                                      "fewest_urls or most_failed"),
        version: Optional[int] = Query(
            default=None,
            description="The version of an earlier answer. If the job history has not "
                        "changed since, the answer is just {unchanged: true}."),
) -> dict:
    # A live view polls; answering "nothing changed" costs no query at all. Running jobs
    # still change (their duration grows), but only their results matter to the view,
    # and every result bumps the version.
    if version is not None and version == jobs.version:
        return {"unchanged": True, "version": jobs.version}
    if sort not in SORTS:
        raise HTTPException(status_code=400, detail=f"Unknown sort '{sort}'. Allowed: "
                                                    f"{', '.join(SORTS)}.")
    criteria = dict(status=job_status, url=url, output_format=output_format,
                    method=method, success=success, since=since, until=until,
                    outcome=outcome)

    # In a thread: a filtered query over a large history can take seconds, and on the
    # event loop that would freeze every retrieval in flight
    def answer() -> dict:
        return {
            "version": jobs.version,
            "jobs": jobs.list_jobs(limit=limit, offset=offset, sort=sort, **criteria),
            "total": jobs.count_jobs(**criteria),
            "stats": jobs.stats(),
            "methods": jobs.known_methods(),
        }
    return await asyncio.to_thread(answer)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = await asyncio.to_thread(jobs.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job '{job_id}'.")
    return job


@router.get("/jobs/{job_id}/content")
async def get_result_content(job_id: str, url: str = Query(...)) -> dict:
    """One result's stored content: the job itself comes without, being megabytes for a
    run of many URLs, and the UI fetches a result's content when it is opened."""
    content = await asyncio.to_thread(jobs.get_content, job_id, url)
    if content is None:
        raise HTTPException(status_code=404, detail=f"No stored content for {url} in job '{job_id}'.")
    return {"url": url, "content": content}


class InterruptRequest(BaseModel):
    url: Optional[str] = None  # Only this URL; the job goes on with the others


@router.post("/jobs/{job_id}/interrupt")
async def interrupt_job(job_id: str, body: Optional[InterruptRequest] = None,
                        principal: Principal = Depends(require_api_key)) -> dict:
    """Interrupts a running job, on behalf of the user: its retrievals stop, what was
    retrieved so far stays, and the job shows as interrupted by the user. With a `url`,
    only that URL's retrieval is stopped (it fails as interrupted) and the job goes on.
    Its own key and admins may do so."""
    url = body.url if body else None
    state = await asyncio.to_thread(jobs.job_state, job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"No job '{job_id}'.")
    if not (principal.is_admin or state["api_key"] == principal.id):
        raise HTTPException(status_code=403, detail="That job runs under another API key.")
    if state["status"] != "running":
        raise HTTPException(status_code=409, detail=f"The job is no longer running "
                                                     f"({state['status']}).")
    if interrupts.interrupt(job_id, url):
        return {"job_id": job_id, "interrupted": True, **({"url": url} if url else {})}
    if url is not None and interrupts.is_live(job_id):
        raise HTTPException(status_code=409, detail="That URL is not being retrieved (any more).")
    # Running in the history, but nothing on this server works on it: an orphaned job
    closed = await asyncio.to_thread(jobs.interrupt_stale, job_id, interrupts.USER)
    return {"job_id": job_id, "interrupted": closed, "orphaned": True}


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    return {"job_id": job_id, "deleted": jobs.delete_job(job_id)}


# --- Statistics -------------------------------------------------------------------

@router.get("/stats/retrievals")
async def retrieval_statistics(
        bucket: str = Query(default="day", description="hour, day or week"),
        group: str = Query(default="outcome",
                           description="method, outcome, kind or key (API key; Admin and Root only)"),
        periods: Optional[int] = Query(
            default=None, ge=1, le=400,
            description="How many buckets, up to the current one (default: 48 hours, "
                        "30 days or 26 weeks)"),
        tz_offset: int = Query(
            default=0, ge=-14 * 60, le=14 * 60,
            description="The viewer's offset from UTC in minutes (east positive), so "
                        "that days and weeks start at local midnight"),
        principal: Principal = Depends(require_api_key),
) -> dict:
    """Past retrievals over time, per method, outcome or API key, for the Statistics view."""
    from ..retrieval_stats import BUCKET_SECONDS, retrieval_stats
    if bucket not in BUCKET_SECONDS:
        raise HTTPException(status_code=400, detail="bucket must be hour, day or week.")
    if group not in ("method", "outcome", "kind", "key"):
        raise HTTPException(status_code=400, detail="group must be method, outcome, kind or key.")
    if group == "key" and not principal.is_admin:
        # The names of the keys are for those who manage them
        raise HTTPException(status_code=403, detail="Grouping by API key needs an Admin or Root API key.")
    # A query over a large range reads many rows: off the event loop
    return await asyncio.to_thread(retrieval_stats, jobs, bucket, group, periods, tz_offset)


# --- Media ------------------------------------------------------------------------

@router.get("/media/{kind}/{identifier}")
async def media(kind: str, identifier: int) -> FileResponse:
    """Serves a media file's bytes, for clients that cannot reach the registry
    directly. Clients on the same machine never come here."""
    item = registry.resolve_item(kind, identifier)
    if item is None:
        raise HTTPException(status_code=404, detail=f"No item <{kind}:{identifier}>.")
    path = item.file_path
    if not path.exists():
        raise HTTPException(status_code=410,
                            detail=f"<{kind}:{identifier}> is registered but its file is gone.")
    return FileResponse(path, filename=path.name)


@router.get("/media", dependencies=ADMIN)
async def media_usage() -> dict:
    return registry.usage()
