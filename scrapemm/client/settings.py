"""Where the client finds its server, and how it wants media delivered."""

import os
from dataclasses import dataclass
from typing import Literal, Optional

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

    @property
    def base_url(self) -> str:
        return self.api_url.rstrip("/")


def _from_environment() -> Settings:
    return Settings(
        api_url=os.getenv("SCRAPEMM_API_URL", DEFAULT_API_URL),
        api_key=os.getenv("SCRAPEMM_API_KEY") or None,
        media_transfer=os.getenv("SCRAPEMM_MEDIA_TRANSFER", "auto"),  # type: ignore[arg-type]
        read_timeout=float(os.getenv("SCRAPEMM_READ_TIMEOUT", DEFAULT_READ_TIMEOUT)),
    )


settings = _from_environment()


def configure(api_url: Optional[str] = None,
              api_key: Optional[str] = None,
              media_transfer: Optional[MediaTransfer] = None,
              read_timeout: Optional[float] = None) -> Settings:
    """Points the client at a scrapeMM server. Anything left out keeps its current
    value, which comes from the SCRAPEMM_API_URL / SCRAPEMM_API_KEY /
    SCRAPEMM_MEDIA_TRANSFER environment variables.

    >>> import scrapemm
    >>> scrapemm.configure(api_url="https://scrapemm.example.org", api_key="...")
    """
    if api_url is not None:
        settings.api_url = api_url
    if api_key is not None:
        settings.api_key = api_key
    if media_transfer is not None:
        if media_transfer not in ("auto", "shared", "link", "download"):
            raise ValueError(f"Unknown media_transfer '{media_transfer}'. Allowed: "
                             f"auto, shared, link, download.")
        settings.media_transfer = media_transfer
    if read_timeout is not None:
        settings.read_timeout = read_timeout
    return settings
