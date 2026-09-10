"""The application's name and the locations where it persists data."""

import os
from pathlib import Path

from platformdirs import user_config_dir

APP_NAME = "scrapeMM"

CONFIG_DIR = Path(user_config_dir(APP_NAME))
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_PATH = CONFIG_DIR / "config.yaml"
BLACKLIST_PATH = CONFIG_DIR / "blacklist.yaml"
