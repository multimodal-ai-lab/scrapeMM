"""The retrieval chain's endpoints: read, change and reset the default chain, list the
methods it can hold with their status, and preview what the chain does for a URL.

See `scrapemm.server.chain` for the model and its semantics.
"""

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from scrapemm.common.paths import APP_NAME
from .. import chain, status as status_module
from ..auth import require_api_key
from ..config import get_config_var, update_config
from ..toggles import is_enabled

logger = logging.getLogger(APP_NAME)

router = APIRouter(prefix="/v1", tags=["chain"], dependencies=[Depends(require_api_key)])

# How long the chain page waits for a method's probe before showing it as still checking
STATUS_WAIT = 4.0


class ChainUpdate(BaseModel):
    chain: list[dict[str, Any]]
    # Seconds of head start per live method before the next runs alongside it; None: off
    hedging_delay: Optional[float] = None
    set_hedging_delay: bool = False  # Whether `hedging_delay` is meant to be applied


class PreviewRequest(BaseModel):
    url: str
    chain: Optional[list[dict[str, Any]]] = None  # Unsaved changes to preview
    exceptions: Optional[list[dict[str, Any]]] = None  # Likewise


class ExceptionsUpdate(BaseModel):
    exceptions: list[dict[str, Any]]  # [{"pattern": "example.com", "methods": [...]}]


@router.get("/chain")
async def read_chain() -> dict:
    return await _describe()


@router.put("/chain")
async def put_chain(body: ChainUpdate) -> dict:
    try:
        chain.save_chain(body.chain)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if body.set_hedging_delay:
        delay = body.hedging_delay
        if delay is not None and delay < 0:
            raise HTTPException(status_code=400, detail="The hedging delay cannot be negative.")
        update_config(hedging_delay=delay or None)
    status_module.invalidate(*status_module.METHOD_KEYS)  # Their enabled state may have changed
    return await _describe()


@router.post("/chain/reset")
async def reset_chain() -> dict:
    chain.reset_chain()
    update_config(hedging_delay=None)
    status_module.invalidate(*status_module.METHOD_KEYS)
    return await _describe()


@router.post("/chain/preview")
async def preview(body: PreviewRequest) -> dict:
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="Enter a URL.")
    if "://" not in url:
        url = "https://" + url
    try:
        return chain.resolve(url, "auto", body.chain, body.exceptions).to_dict() | {"url": url}
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/chain/exceptions")
async def read_exceptions() -> dict:
    return _describe_exceptions()


@router.put("/chain/exceptions")
async def put_exceptions(body: ExceptionsUpdate) -> dict:
    try:
        chain.save_exceptions(body.exceptions)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _describe_exceptions()


@router.post("/chain/exceptions/reset")
async def reset_exceptions() -> dict:
    chain.reset_exceptions()
    return _describe_exceptions()


def _describe_exceptions() -> dict:
    """The exceptions, each flagged as a default, a changed default or the user's own;
    plus the defaults, and the defaults the user removed."""
    defaults = {e.pattern: e.methods for e in chain.default_exceptions()}
    current = chain.configured_exceptions()
    patterns = {e.pattern for e in current}
    return {
        "exceptions": [e.to_dict() | {
            "origin": ("default" if defaults.get(e.pattern) == e.methods
                       else "modified" if e.pattern in defaults else "custom")}
            for e in current],
        "defaults": [e.to_dict() for e in chain.default_exceptions()],
        "removed_defaults": [p for p in defaults if p not in patterns],
        "live_methods": chain.LIVE_KEYS,
    }


async def _describe() -> dict:
    steps = chain.configured_chain()
    defaults = chain.default_chain()
    statuses = await asyncio.gather(*(_status(m.key) for m in chain.METHODS))
    methods = []
    for info, status in zip(chain.METHODS, statuses):
        methods.append({
            "key": info.key, "name": info.name, "label": info.label, "stage": info.stage,
            "description": info.description, "speed": info.speed, "cost": info.cost,
            "local": info.local, "available": info.available, **status,
        })
    return {
        "chain": [s.to_dict() for s in steps],
        "defaults": [s.to_dict() for s in defaults],
        "is_default": [s.to_dict() for s in steps] == [s.to_dict() for s in defaults]
                      and get_config_var("hedging_delay") in (None, 0),
        "methods": methods,
        "hedging_delay": get_config_var("hedging_delay"),
        "routes": chain.DOMAIN_ROUTES,
    }


async def _status(key: str) -> dict:
    """What the chain page shows about a method: its state (ready, limited, unconfigured,
    error, disabled, unavailable, checking) and a line of detail."""
    info = chain.METHOD_INFO[key]
    if not info.available:
        return {"state": "unavailable", "detail": "Not available yet: coming in a later version."}
    if not is_enabled(key):
        return {"state": "disabled", "detail": "Switched off: skipped for every URL."}
    if key in status_module.METHOD_KEYS:
        return await _probed(key)
    if key == "integrations":
        return await _integrations_summary()
    if key == "wayback" and not is_enabled("Internet Archive"):
        return {"state": "error", "detail": "Needs the Internet Archive integration, which is "
                                            "switched off on the dashboard."}
    if key == "perma_cc" and not is_enabled("Perma.cc"):
        return {"state": "error", "detail": "Needs the Perma.cc integration, which is "
                                            "switched off on the dashboard."}
    return {"state": "ready", "detail": ""}  # Needs no configuration


async def _probed(key: str) -> dict:
    try:
        status = await asyncio.wait_for(asyncio.shield(status_module.check(key)), STATUS_WAIT)
    except asyncio.TimeoutError:
        return {"state": "checking", "detail": "Still checking..."}
    except Exception as e:
        return {"state": "error", "detail": f"{type(e).__name__}: {e}"}
    detail = status.detail
    if status.missing_secrets:
        detail = f"Missing secret(s): {', '.join(status.missing_secrets)}."
    return {"state": status.state, "detail": detail}


async def _integrations_summary() -> dict:
    """The integrations as one step: how many of them are ready. Uses the dashboard's last
    probes; integrations nobody has probed yet are not probed here (they log in to
    services, which a settings page should not trigger)."""
    methods = [m for m in status_module.all_methods() if m["kind"] == "integration"]
    states = [(status_module.cached_status(m["key"])[0], m) for m in methods]
    known = [(s, m) for s, m in states if s is not None]
    ready = [m["name"] for s, m in known if s.state in ("ready", "limited")]
    off = [m["name"] for m in methods if not is_enabled(m["key"])]
    if not known:
        return {"state": "ready", "detail": f"{len(methods)} integrations; open the dashboard "
                                            f"to check each."}
    detail = f"{len(ready)} of {len(methods)} ready"
    if off:
        detail += f"; switched off: {', '.join(off)}"
    return {"state": "ready" if ready else "error", "detail": detail + "."}
