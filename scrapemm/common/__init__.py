"""Everything the client and the server share.

This package is deliberately import-light: it must not pull in Playwright, yt-dlp or
any other server-side dependency, because the published `scrapeMM` package is the
client alone (the server ships as a Docker image).
"""

import io
import logging
import os
import sys

from .exceptions import (AccessBlockedError, CaptchaEncounteredError, DiskFull,
                         QuotaExceededError, RateLimitError, RetrievalFailed,
                         TargetUnavailableError, UnsupportedDomainError,
                         ServerError, exception_from_wire, exception_to_wire)
from .paths import APP_NAME
from .scraping_response import (ScrapingResponse, ScrapedContent,
                                OutputFormat, OUTPUT_FORMATS)

# Set up logger. The level is DEBUG unless SCRAPEMM_LOG_LEVEL says otherwise -- the
# server runs unattended, where debug-level chatter is rarely what you want.
logger = logging.getLogger(APP_NAME)
logger.setLevel(os.getenv("SCRAPEMM_LOG_LEVEL", "DEBUG").upper())


def _utf8_stdout():
    """Returns stdout as a UTF-8 stream. Needed because log messages contain emojis
    which consoles with another default encoding (e.g. cp1252 on Windows) cannot print."""
    if (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "") == "utf8":
        return sys.stdout
    try:
        return io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                                line_buffering=True)
    except (AttributeError, ValueError):
        return sys.stdout  # stdout has no underlying binary buffer, e.g. when captured


# Only add handler if none exists (avoid duplicate logs on rerun)
if not logger.hasHandlers():
    handler = logging.StreamHandler(_utf8_stdout())
    formatter = logging.Formatter('[%(levelname)s]: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
