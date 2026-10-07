"""The API keys: who is calling (`/v1/me`), and managing the keys (Admin and Root).

A key manages only keys of a lower role (see `scrapemm.server.auth`). The root key is
listed along with the others, but only regenerated, never renamed or revoked.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from scrapemm.common.paths import APP_NAME
from ..auth import (ADMIN, CLIENT, MAX_NAME_LENGTH, ROOT, ROOT_ID, Principal,
                    api_key_from_environment, key_store, regenerate_api_key,
                    require_admin, require_api_key, require_root)

logger = logging.getLogger(APP_NAME)

router = APIRouter(prefix="/v1", tags=["api keys"], dependencies=[Depends(require_api_key)])


class NewKey(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    role: Literal["admin", "client"] = CLIENT


class KeyName(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)


@router.get("/me")
async def me(principal: Principal = Depends(require_api_key)) -> dict:
    """The key this request carries: what the web UI shows and hides by."""
    return {"id": principal.id, "name": principal.name, "role": principal.role}


@router.get("/api-keys")
async def list_keys(principal: Principal = Depends(require_admin)) -> dict:
    """Every key, root first. Never a key itself: the server does not have them."""
    root = {"id": ROOT_ID, "name": "Root", "role": ROOT, "hint": None, "created_at": None,
            "last_used_at": None, "from_environment": api_key_from_environment()}
    keys = sorted(key_store().list(), key=lambda k: (k["role"] != ADMIN, k["created_at"]))
    return {"keys": [root, *keys], "me": principal.id}


@router.post("/api-keys")
async def create_key(body: NewKey, principal: Principal = Depends(require_admin)) -> dict:
    """Returns the new key once; it cannot be looked up later."""
    if not principal.outranks(body.role):
        raise HTTPException(status_code=403, detail=f"Only Root can create {body.role} keys.")
    try:
        key, token = key_store().create(_clean(body.name), body.role)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"key": key, "token": token}


@router.patch("/api-keys/{key_id}")
async def rename_key(key_id: str, body: KeyName,
                     principal: Principal = Depends(require_admin)) -> dict:
    _manageable(key_id, principal)
    try:
        return {"key": key_store().rename(key_id, _clean(body.name))}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.delete("/api-keys/{key_id}")
async def revoke_key(key_id: str, principal: Principal = Depends(require_admin)) -> dict:
    _manageable(key_id, principal)
    key_store().revoke(key_id)
    return {"revoked": key_id}


@router.post("/api-keys/root/regenerate", dependencies=[Depends(require_root)])
async def regenerate_root_key() -> dict:
    """Returns the new root key once, so the caller can switch over to it."""
    try:
        return {"token": regenerate_api_key()}
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


def _manageable(key_id: str, principal: Principal) -> None:
    """Refuses unless the key exists and the caller outranks it."""
    if key_id == ROOT_ID:
        raise HTTPException(status_code=403, detail="The root key can only be regenerated.")
    key = key_store().get(key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="There is no such key.")
    if not principal.outranks(key["role"]):
        raise HTTPException(status_code=403,
                            detail=f"Only Root can manage {key['role']} keys.")


def _clean(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise HTTPException(status_code=422, detail="A key needs a name.")
    return name
