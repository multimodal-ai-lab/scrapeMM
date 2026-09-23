"""scrapeMM: multimodal web retrieval.

This package is the **client**. It does not scrape anything itself; it asks a scrapeMM
server to, and turns the answer back into the `ScrapingResponse` objects that callers
work with. Run a server with `docker compose up -d` (see the README) and point the
client at it:

    import asyncio, scrapemm

    scrapemm.configure(api_url="http://localhost:8080", api_key="...")
    result = asyncio.run(scrapemm.retrieve("https://example.com"))

Configuration of the server itself -- API secrets, Firecrawl endpoints, the domain
blacklist, the Archive.today CAPTCHA -- happens in the server's web UI, not from here.
"""

from .common import (APP_NAME, AccessBlockedError, CaptchaEncounteredError, DiskFull,
                     QuotaExceededError, RateLimitError, RetrievalFailed, ScrapedContent,
                     ScrapingResponse, ServerError, TargetUnavailableError,
                     UnsupportedDomainError, logger)
from .client import Settings, configure, retrieve, settings

__version__ = "1.0.0"

__all__ = [
    "retrieve",
    "configure",
    "settings",
    "Settings",
    "ScrapingResponse",
    "ScrapedContent",
    "AccessBlockedError",
    "CaptchaEncounteredError",
    "DiskFull",
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
