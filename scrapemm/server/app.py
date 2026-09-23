"""The scrapeMM server application.

One FastAPI app serves both the API under `/v1` and the built Nuxt UI at `/`, so a
deployment is one container on one port -- which is also why the UI is a static SPA
rather than a second service with its own runtime.

Run it with `docker compose up -d`, or directly during development:

    uvicorn scrapemm.server.app:app --port 8080
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from scrapemm.common.paths import APP_NAME
from . import registry
from .api import ROUTERS
from .auth import api_key
from .jobs import jobs
from .secrets import log_summary
from .version import __version__

logger = logging.getLogger(APP_NAME)

# Where the built UI lands in the image. Absent during API-only development, in which
# case `/` simply says so instead of serving a page.
UI_DIR = Path(os.getenv("SCRAPEMM_UI_DIR", Path(__file__).parent / "ui"))

# Nuxt's dev server runs on its own origin and proxies /v1 here; in production the UI
# is served from this very origin, so no cross-origin access is needed at all.
DEV_ORIGINS = [o.strip() for o in os.getenv("SCRAPEMM_CORS_ORIGINS", "").split(",")
               if o.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"🚀 scrapeMM server {__version__} starting up.")
    api_key()  # Generates and prints one if the deployment did not set it
    log_summary()
    registry.fingerprint()  # Stamp the media registry so clients can recognise it
    _warn_if_exposed()
    jobs.prune()
    # Fill the caches the dashboard depends on, in the background. Starting Playwright's
    # driver and walking the media tree cost a couple of seconds between them, and there
    # is no reason for the first person to open the dashboard to be the one who pays it.
    warmup = asyncio.create_task(_warm_caches())
    yield
    warmup.cancel()
    jobs.close()
    logger.info("scrapeMM server stopped.")


async def _warm_caches() -> None:
    """Pre-computes the slow, rarely-changing parts of the dashboard's environment."""
    try:
        from . import status
        await status._playwright_status()
        await asyncio.to_thread(registry.usage)
    except asyncio.CancelledError:
        raise
    except Exception:
        # A warm-up is an optimisation; failing it must not affect the server at all.
        logger.debug("Warming the dashboard caches failed.", exc_info=True)


def _warn_if_exposed() -> None:
    """Secrets are submitted through the UI, so a server reachable from outside
    localhost without TLS in front of it is handing them over in the clear."""
    host = os.getenv("SCRAPEMM_HOST", "0.0.0.0")
    behind_tls = os.getenv("SCRAPEMM_BEHIND_TLS", "").lower() in ("1", "true", "yes")
    if host not in ("127.0.0.1", "localhost") and not behind_tls:
        logger.warning(
            "⚠️ This server listens beyond localhost without declaring TLS in front of "
            "it. Secrets submitted in the web UI would travel unencrypted. Put a "
            "reverse proxy with HTTPS in front and set SCRAPEMM_BEHIND_TLS=1, or keep "
            "the port bound to localhost.")


def create_app() -> FastAPI:
    app = FastAPI(
        title="scrapeMM",
        version=__version__,
        description="Multimodal web retrieval: social media, internet archives and the "
                    "open web, as text plus the media downloaded from the page.",
        lifespan=lifespan,
    )

    if DEV_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=DEV_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    for router in ROUTERS:
        app.include_router(router)

    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    async def healthz() -> dict:
        """Unauthenticated: it is what the container's health check calls."""
        return {"status": "ok", "version": __version__}

    _mount_ui(app)
    return app


def _mount_ui(app: FastAPI) -> None:
    """Serves the built Nuxt UI."""
    if not UI_DIR.is_dir():
        @app.get("/", include_in_schema=False)
        async def no_ui() -> JSONResponse:
            return JSONResponse({
                "message": "The scrapeMM API is running, but no web UI is built here.",
                "api": "/v1",
                "docs": "/docs",
            })
        return

    if (UI_DIR / "_nuxt").is_dir():
        app.mount("/_nuxt", StaticFiles(directory=UI_DIR / "_nuxt"), name="nuxt")

    index = UI_DIR / "index.html"

    # HEAD as well as GET: reverse proxies and uptime checks probe the root with HEAD,
    # and FastAPI does not derive it from a GET route on its own.
    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def ui(path: str) -> FileResponse:
        """Serves a real file where there is one, and index.html otherwise -- the SPA
        owns routes like /jobs/abc123, which exist in the browser but not on disk, and
        a reload of one of those must not 404."""
        candidate = (UI_DIR / path).resolve()
        # Refuse anything that escapes the UI directory, however it was spelled
        if UI_DIR.resolve() in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)


app = create_app()
