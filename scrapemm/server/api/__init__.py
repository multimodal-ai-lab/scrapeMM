"""The HTTP API. Every route lives under /v1 and needs the bearer token."""

from .admin import router as admin_router
from .captcha import router as captcha_router
from .chain import router as chain_router
from .retrieve import router as retrieve_router
from .test import router as test_router
from .vnc import router as vnc_router

ROUTERS = [retrieve_router, admin_router, chain_router, captcha_router, test_router, vnc_router]

__all__ = ["ROUTERS"]
