"""The proxy's endpoints: read, change and test it (see `scrapemm.server.proxy`). The
username and password are write-only: responses only say whether they are set."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import proxy
from ..status import proxy_status
from ..auth import require_admin

# Part of Settings: Admin and Root only
router = APIRouter(prefix="/v1", tags=["proxy"], dependencies=[Depends(require_admin)])


class ProxyUpdate(BaseModel):
    url: str = ""  # scheme://host:port; empty removes the proxy
    enabled: bool = True
    username: Optional[str] = None  # None keeps the stored one, "" removes it
    password: Optional[str] = None  # Likewise
    clear_credentials: bool = False  # Removes both


class ProxyTest(BaseModel):
    # Unsaved values to test; whatever is left out comes from the stored proxy
    url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


def _describe() -> dict:
    status = proxy_status()  # With until when YouTube is paused for this server
    entries = [{key: status[key] for key in ("url", "enabled", "username_set", "password_set")}
               ] if status["configured"] else []
    return {"proxies": entries, "status": status}


@router.get("/proxy")
async def read_proxy() -> dict:
    return _describe()


@router.put("/proxy")
async def put_proxy(body: ProxyUpdate) -> dict:
    try:
        proxy.save(body.url, body.enabled, username=body.username, password=body.password,
                   clear_credentials=body.clear_credentials)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:  # The secrets store cannot be read
        raise HTTPException(status_code=409, detail=str(e))
    return _describe()


@router.post("/proxy/test")
async def test_proxy(body: ProxyTest) -> dict:
    stored = proxy.configured()
    url = body.url if body.url is not None else (stored.url if stored else "")
    try:
        url = proxy.validate_url(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # The stored login only for the stored proxy: never sent to a different address
    same = stored is not None and stored.url == url
    username = body.username if body.username is not None else (stored.username if same else None)
    password = body.password if body.password is not None else (stored.password if same else None)
    return await proxy.test(proxy.Proxy(url=url, username=username or None,
                                        password=password or None))
