"""Where the client finds its server, and how it wants media delivered.

`configure()` saves what it is given to a per-user file, so a machine is set up once
rather than in every script. Precedence, lowest first: that file, then the SCRAPEMM_*
environment variables, then `configure()` calls in the running process.
"""

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from scrapemm.common.paths import APP_NAME

logger = logging.getLogger(APP_NAME)

# How the media files of a retrieved page reach the client:
#   "auto"     - work it out per server (the default, and almost always right)
#   "shared"   - client and server use the very same ezMM registry; references are
#                already valid here and nothing has to be done at all
#   "link"     - the server's registry is reachable on this filesystem; its files are
#                registered here by path, so no bytes are copied
#   "download" - fetch the bytes over the API into this client's own registry
MediaTransfer = Literal["auto", "shared", "link", "download"]

DEFAULT_API_URL = "http://localhost:8080"

# Retrieval of a large batch legitimately takes many minutes, and the server streams
# results as they land, so the client does not impose a total deadline -- only a limit
# on how long a *connection* may stall without producing a line.
DEFAULT_READ_TIMEOUT = 15 * 60


@dataclass
class Settings:
    api_url: str = DEFAULT_API_URL
    api_key: Optional[str] = None
    media_transfer: MediaTransfer = "auto"
    read_timeout: float = DEFAULT_READ_TIMEOUT

    def __repr__(self) -> str:
        # Tracebacks print this object; the key must not end up in a CI log
        key = "set" if self.api_key else None
        return (f"Settings(api_url={self.api_url!r}, api_key={key!r}, "
                f"media_transfer={self.media_transfer!r}, read_timeout={self.read_timeout!r})")

    @property
    def base_url(self) -> str:
        # Tolerate the API prefix given along with the server: every route adds it anyway
        url = self.api_url.rstrip("/")
        return url.removesuffix("/v1")


# Each saved setting and the environment variable that overrides it
ENV_VARS = {
    "api_url": "SCRAPEMM_API_URL",
    "api_key": "SCRAPEMM_API_KEY",
    "media_transfer": "SCRAPEMM_MEDIA_TRANSFER",
    "read_timeout": "SCRAPEMM_READ_TIMEOUT",
}


def _config_dir() -> Path:
    if sys.platform == "win32" and os.getenv("APPDATA"):
        return Path(os.environ["APPDATA"]) / APP_NAME
    return Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME


CONFIG_PATH = _config_dir() / "client.json"


def _load_saved() -> dict[str, Any]:
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Ignoring the unreadable client configuration at {CONFIG_PATH}: {e}")
        return {}
    return {k: v for k, v in saved.items() if k in ENV_VARS} if isinstance(saved, dict) else {}


def _save(values: dict[str, Any]) -> None:
    """Merges `values` into the saved configuration."""
    merged = {**_load_saved(), **values}
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        os.chmod(CONFIG_PATH, 0o600)  # It holds the API key
    except OSError as e:
        logger.warning(f"Could not save the client configuration to {CONFIG_PATH}: {e}. "
                       f"It applies to this process only.")
        return
    overridden = [ENV_VARS[k] for k in values if os.getenv(ENV_VARS[k])]
    if overridden:
        logger.warning(f"Saved to {CONFIG_PATH}, but {', '.join(overridden)} is set in the "
                       f"environment and takes precedence in new processes.")


def _load() -> Settings:
    values = _load_saved()
    for name, var in ENV_VARS.items():
        if env := os.getenv(var):
            values[name] = env
    return Settings(
        api_url=values.get("api_url") or DEFAULT_API_URL,
        api_key=values.get("api_key") or None,
        media_transfer=values.get("media_transfer") or "auto",
        read_timeout=float(values.get("read_timeout") or DEFAULT_READ_TIMEOUT),
    )


settings = _load()


def configure(api_url: Optional[str] = None,
              api_key: Optional[str] = None,
              media_transfer: Optional[MediaTransfer] = None,
              read_timeout: Optional[float] = None,
              persist: bool = True) -> Settings:
    """Points the client at a scrapeMM server. Anything left out keeps its current
    value. What is given is also saved for future processes on this machine (see
    CONFIG_PATH); pass persist=False to change this process only.

    >>> import scrapemm
    >>> scrapemm.configure(api_url="https://scrapemm.example.org", api_key="...")
    """
    if media_transfer is not None and media_transfer not in ("auto", "shared", "link",
                                                             "download"):
        raise ValueError(f"Unknown media_transfer '{media_transfer}'. Allowed: "
                         f"auto, shared, link, download.")
    given = {name: value for name, value in [("api_url", api_url), ("api_key", api_key),
                                             ("media_transfer", media_transfer),
                                             ("read_timeout", read_timeout)]
             if value is not None}
    for name, value in given.items():
        setattr(settings, name, value)
    if persist and given:
        _save(given)
    return settings
