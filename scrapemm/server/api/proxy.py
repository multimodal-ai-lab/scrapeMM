"""The proxies' endpoints: read, change and test them (see `scrapemm.server.proxy`). The
username and password are write-only: responses only say whether they are set."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import proxy
from ..status import proxy_status
from ..auth import require_admin

# Part of Settings: Admin and Root only
router = APIRouter(prefix="/v1", tags=["proxy"], dependencies=[Depends(require_admin)])


class Endpoint(BaseModel):
    url: str  # [scheme://]host:port
    enabled: bool = True


class ProxyUpdate(BaseModel):
    proxies: list[Endpoint] = []  # In their order; empty removes all (and the login)
    username: Optional[str] = None  # None keeps the stored one, "" removes it
    password: Optional[str] = None  # Likewise
    clear_credentials: bool = False  # Removes both


class ProxyTest(BaseModel):
    url: str  # Possibly not saved yet
    # Unsaved credentials to test with; left out, the stored ones
    username: Optional[str] = None
    password: Optional[str] = None


def _describe() -> dict:
    proxies = proxy.configured()
    return {"proxies": proxy.describe(),
            "username_set": bool(proxies and proxies[0].username),
            "password_set": bool(proxies and proxies[0].password),
            # With until when YouTube is paused for this server
            "status": proxy_status()}


@router.get("/proxy")
async def read_proxy() -> dict:
    return _describe()


@router.put("/proxy")
async def put_proxy(body: ProxyUpdate) -> dict:
    try:
        proxy.save([e.model_dump() for e in body.proxies], username=body.username,
                   password=body.password, clear_credentials=body.clear_credentials)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:  # The secrets store cannot be read
        raise HTTPException(status_code=409, detail=str(e))
    return _describe()


@router.post("/proxy/test")
async def test_proxy(body: ProxyTest) -> dict:
    """Tests one endpoint, saved or not."""
    try:
        url = proxy.validate_url(body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # The stored login only for a host of a stored endpoint (a provider's endpoints differ
    # in the port): never sent to a different address
    stored = proxy.configured()
    candidate = proxy.Proxy(url=url)
    same = next((p for p in stored if p.host == candidate.host), None)
    username = body.username if body.username is not None else (same.username if same else None)
    password = body.password if body.password is not None else (same.password if same else None)
    # Kept as the endpoint's health only when tested as it is stored
    keep = body.username is None and body.password is None
    return await proxy.test(proxy.Proxy(url=url, username=username or None,
                                        password=password or None), keep=keep)


@router.post("/proxy/test-all")
async def test_all_proxies() -> dict:
    """Tests every saved endpoint; their results become their health."""
    await proxy.test_all()
    return _describe()
