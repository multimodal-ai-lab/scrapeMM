import logging
import sys

import scrapemm.common
from .common import APP_NAME, set_wait_on_rate_limit, RateLimitError, ContentNotFoundError
from .integrations import Telegram, X
from .retrieval import retrieve
from .secrets import configure_secrets
from .util import run_command

# Set up logger
logger = logging.getLogger(APP_NAME)
logger.setLevel(logging.DEBUG)

# Only add handler if none exists (avoid duplicate logs on rerun)
if not logger.hasHandlers():
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('[%(levelname)s]: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False

# Check if ffmpeg is available.
try:
    run_command(["ffmpeg", "-version"])
    ffmpeg_available = True
except FileNotFoundError:
    logger.warning("FFmpeg not found. Skipping video normalization.")
    ffmpeg_available = False
