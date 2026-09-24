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
from .. import registry, status as status_module
from ..auth import api_key_from_environment, regenerate_api_key, require_api_key
from ..blacklist import blacklist
from ..cache import cache
from ..config import SETTINGS, get_config, update_config
from ..jobs import jobs
from ..toggles import set_enabled
from ..secrets import (MANAGED_SECRETS, SECRETS, describe_secrets, remove_secret,
                       rotate_key, set_secret)
from ..version import __version__

logger = logging.getLogger(APP_NAME)

router = APIRouter(prefix="/v1", tags=["admin"], dependencies=[Depends(require_api_key)])


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


@router.put("/integrations/{name}/enabled")
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


# --- Secrets ----------------------------------------------------------------------

class SecretValue(BaseModel):
    value: str


@router.get("/secrets")
async def list_secrets() -> dict:
    return {"secrets": describe_secrets()}


@router.put("/secrets/{name}")
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
    # The dashboard should show the effect at once, not after the status TTL lapses
    status_module.invalidate(*status_module.secrets_to_integrations(name))
    logger.info(f"Secret '{name}' was set through the API.")
    return {"name": name, "is_set": True}


@router.delete("/secrets/{name}")
async def delete_secret(name: str) -> dict:
    if name not in SECRETS:
        raise HTTPException(status_code=404, detail=f"Unknown secret '{name}'.")
    removed = remove_secret(name)
    status_module.invalidate(*status_module.secrets_to_integrations(name))
    return {"name": name, "removed": removed, "is_set": False}


@router.post("/secrets/rotate-key")
async def rotate_secrets_key() -> dict:
    try:
        rotate_key()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"rotated": True}


# --- API key ----------------------------------------------------------------------

@router.get("/api-key")
async def describe_api_key() -> dict:
    """Never the key itself: only whether the UI may regenerate it."""
    return {"from_environment": api_key_from_environment()}


@router.post("/api-key/regenerate")
async def regenerate_key() -> dict:
    """Returns the new key once, so the caller can switch over to it."""
    try:
        return {"api_key": regenerate_api_key()}
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


# --- Configuration ----------------------------------------------------------------

@router.get("/config")
async def read_config() -> dict:
    return {"config": get_config(),
            "settings": {name: kind.__name__ for name, kind in SETTINGS.items()}}


@router.patch("/config")
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

@router.get("/cache")
async def read_cache() -> dict:
    return {"entries": len(cache), "ttl": cache.ttl}


@router.post("/cache/clear")
async def clear_the_cache() -> dict:
    entries = len(cache)
    cache.clear()
    return {"cleared": entries}


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
        since: Optional[float] = Query(default=None,
                                       description="UNIX timestamp, inclusive"),
        until: Optional[float] = Query(default=None,
                                       description="UNIX timestamp, inclusive"),
) -> dict:
    criteria = dict(status=job_status, url=url, output_format=output_format,
                    method=method, success=success, since=since, until=until)
    return {
        "jobs": jobs.list_jobs(limit=limit, offset=offset, **criteria),
        "total": jobs.count_jobs(**criteria),
        "stats": jobs.stats(),
        "methods": jobs.known_methods(),
    }


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job '{job_id}'.")
    return job


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    return {"job_id": job_id, "deleted": jobs.delete_job(job_id)}


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


@router.get("/media")
async def media_usage() -> dict:
    return registry.usage()
