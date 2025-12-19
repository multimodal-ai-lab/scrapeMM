import logging
import os
import sys
from pathlib import Path

import yaml
from platformdirs import user_config_dir

from .exceptions import RateLimitError, ContentNotFoundError
from .scraping_response import ScrapingResponse

APP_NAME = "scrapeMM"

# Set up config directory
CONFIG_DIR = Path(user_config_dir(APP_NAME))
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_PATH = CONFIG_DIR / "config.yaml"

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

WAIT_ON_RATE_LIMIT = False


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f) or {}
    else:
        return {}


def update_config(**kwargs):
    _config.update(kwargs)
    yaml.dump(_config, open(CONFIG_PATH, "w"))


def get_config_var(name: str, default=None) -> str:
    return _config.get(name, default)


def set_wait_on_rate_limit(wait: bool):
    """Set whether to wait on rate limits (particularly relevant for X API).
    Will result in a RateLimitError otherwise."""
    global WAIT_ON_RATE_LIMIT
    WAIT_ON_RATE_LIMIT = wait


# Load config
_config = load_config()
