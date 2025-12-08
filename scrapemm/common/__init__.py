import os
from pathlib import Path

import yaml
from platformdirs import user_config_dir

from .scraping_response import ScrapingResponse
from .exceptions import RateLimitError, ContentNotFoundError

APP_NAME = "scrapeMM"

# Set up config directory
CONFIG_DIR = Path(user_config_dir(APP_NAME))
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_PATH = CONFIG_DIR / "config.yaml"

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
