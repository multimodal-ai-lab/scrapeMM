"""scrapeMM: multimodal web retrieval.

This package is the **client**. It does not scrape anything itself; it asks a scrapeMM
server to, and turns the answer back into the `ScrapingResponse` objects that callers
work with. Run a server with `docker compose up -d` (see the README) and point the
client at it:

    import asyncio, scrapemm

    scrapemm.configure(api_url="http://localhost:8080", api_key="...")
    result = asyncio.run(scrapemm.retrieve("https://example.com"))

Web search, through search APIs like Serper, lives in `scrapemm.search`.

Configuration of the server itself -- API secrets, Firecrawl endpoints, the domain
blacklist, the Archive.today CAPTCHA -- happens in the server's web UI, not from here.
"""

from .common import (APP_NAME, AccessBlockedError, CaptchaEncounteredError, DiskFull,
                     DomainBlacklistedError, PaywallError, QuotaExceededError,
                     RateLimitError, RetrievalFailed, ScrapedContent, ScrapingResponse, ServerError,
                     TargetUnavailableError, UnsupportedDomainError, logger)
from .client import Settings, configure, retrieve, settings
from . import search

try:  # pyproject.toml is the single source of the version
    from importlib.metadata import version as _version
    __version__ = _version("scrapeMM")
except Exception:  # Running from a source tree that was never installed
    try:
        import tomllib
        from pathlib import Path
        with open(Path(__file__).parent.parent / "pyproject.toml", "rb") as _f:
            __version__ = tomllib.load(_f)["project"]["version"]
    except Exception:
        __version__ = "unknown"

__all__ = [
    "retrieve",
    "search",
    "configure",
    "settings",
    "Settings",
    "ScrapingResponse",
    "ScrapedContent",
    "AccessBlockedError",
    "CaptchaEncounteredError",
    "DiskFull",
    "DomainBlacklistedError",
    "PaywallError",
    "QuotaExceededError",
    "RateLimitError",
    "RetrievalFailed",
    "ServerError",
    "TargetUnavailableError",
    "UnsupportedDomainError",
    "APP_NAME",
    "logger",
    "__version__",
]
