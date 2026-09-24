"""Bearer-token authentication for the API and the web UI.

One shared token, taken from SCRAPEMM_API_KEY. If none is set the server generates one
on first start and keeps it in the config directory, so a deployment is never
accidentally wide open -- the token is printed once at startup for the admin to copy
into the UI.
"""

import logging
import os
import secrets as secrets_module
from typing import Optional

from fastapi import Header, HTTPException, Query, status

from scrapemm.common.paths import APP_NAME
from .paths import CONFIG_DIR

logger = logging.getLogger(APP_NAME)

API_KEY_PATH = CONFIG_DIR / "api_key"

_api_key: Optional[str] = None


def api_key() -> str:
    """The token this server accepts, generated on first use."""
    global _api_key
    if _api_key is not None:
        return _api_key

    if env_key := os.getenv("SCRAPEMM_API_KEY"):
        _api_key = env_key.strip()
        return _api_key

    try:
        stored = API_KEY_PATH.read_text(encoding="utf-8").strip()
        if stored:
            _api_key = stored
            return _api_key
    except OSError:
        pass

    _api_key = _generate()
    logger.warning("🔑 No SCRAPEMM_API_KEY was set, so a new API key was generated.")
    return _api_key


def log_api_key() -> None:
    """Prints the key on every startup, so the admin can always find it in the logs."""
    key = api_key()
    source = "from SCRAPEMM_API_KEY" if api_key_from_environment() else f"stored in {API_KEY_PATH}"
    logger.info(f"🔑 API key ({source}): {key}\n"
                f"   Enter it in the web UI, or set SCRAPEMM_API_KEY in your .env.")


def api_key_from_environment() -> bool:
    return bool(os.getenv("SCRAPEMM_API_KEY"))


def regenerate_api_key() -> str:
    """Replaces the token with a fresh one; the old one stops working immediately."""
    global _api_key
    if api_key_from_environment():
        raise RuntimeError("The API key is set by SCRAPEMM_API_KEY in the environment, "
                           "which would override a regenerated one on the next start. "
                           "Change it there instead, or remove it to manage the key here.")
    _api_key = _generate()
    logger.warning("🔑 The API key was regenerated. Clients using the old one are now "
                   "rejected.")
    return _api_key


def _generate() -> str:
    """A new token, persisted so that it survives restarts."""
    key = secrets_module.token_urlsafe(32)
    try:
        API_KEY_PATH.write_text(key, encoding="utf-8")
        os.chmod(API_KEY_PATH, 0o600)
    except OSError:
        logger.warning(f"Could not persist the API key to {API_KEY_PATH}; a new one "
                       f"will be generated on the next start.")
    return key


def _token_from(authorization: Optional[str], token: Optional[str]) -> Optional[str]:
    """Reads the token from the Authorization header, or from a query parameter -- the
    latter because a browser opening a WebSocket cannot set headers."""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value.strip()
    return token


async def require_api_key(authorization: Optional[str] = Header(default=None),
                          token: Optional[str] = Query(default=None)) -> None:
    """FastAPI dependency guarding every /v1 route."""
    provided = _token_from(authorization, token)
    # Constant-time comparison: the token is a bearer credential, so a timing oracle
    # on it is worth avoiding even though the exposure is small.
    if not provided or not secrets_module.compare_digest(provided, api_key()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or missing API key.",
                            headers={"WWW-Authenticate": "Bearer"})


def check_token(provided: Optional[str]) -> bool:
    """Same check, for places that are not FastAPI dependencies (the VNC WebSocket)."""
    return bool(provided) and secrets_module.compare_digest(provided, api_key())
