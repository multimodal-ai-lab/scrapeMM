import os
from pathlib import Path

import yaml
from platformdirs import user_config_dir

from .scraping_response import ScrapingResponse

APP_NAME = "scrapeMM"

# Set up config directory
CONFIG_DIR = Path(user_config_dir(APP_NAME))
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_PATH = CONFIG_DIR / "config.yaml"


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


# Load config
_config = load_config()
