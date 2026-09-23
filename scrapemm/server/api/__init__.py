"""The HTTP API. Every route lives under /v1 and needs the bearer token."""

from .admin import router as admin_router
from .archive_today import router as archive_today_router
from .retrieve import router as retrieve_router
from .vnc import router as vnc_router

ROUTERS = [retrieve_router, admin_router, archive_today_router, vnc_router]

__all__ = ["ROUTERS"]
