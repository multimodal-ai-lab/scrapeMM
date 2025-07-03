import logging
import os
import sys
from pathlib import Path
from platformdirs import user_config_dir

import yaml

from scrapemm.util import get_domain

APP_NAME = "scrapeMM"

# Set up logger
logger = logging.getLogger(APP_NAME)
logger.setLevel(logging.DEBUG)

# Only add handler if none exists (avoid duplicate logs on rerun)
if not logger.hasHandlers():
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('[%(levelname)s]: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

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


def is_no_bot_site(url: str) -> bool:
    """Checks if the URL belongs to a known unsupported website."""
    domain = get_domain(url)
    return domain is None or domain.endswith(".gov") or domain in no_bot_domains


def read_urls_from_file(file_path):
    with open(file_path, 'r') as f:
        return f.read().splitlines()


def update_config(**kwargs):
    _config.update(kwargs)
    yaml.dump(_config, open(CONFIG_PATH, "w"))


def get_config_var(name: str, default=None) -> str:
    return _config.get(name, default)


# Load config
_config = load_config()

no_bot_domains_file = Path(__file__).parent / "no_bot_domains.txt"
no_bot_domains = read_urls_from_file(no_bot_domains_file)
