"""The application's name and the locations where it persists data."""

import os
from pathlib import Path

from platformdirs import user_config_dir

APP_NAME = "scrapeMM"

CONFIG_DIR = Path(user_config_dir(APP_NAME))
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_PATH = CONFIG_DIR / "config.yaml"
BLACKLIST_PATH = CONFIG_DIR / "blacklist.yaml"

# Profile of the shared headed browser. Keeping it across runs is what makes a session
# that passed a bot check last: the cookies the server refreshes on every response stay
# in the profile, and the browser keeps looking like the same returning client instead
# of a brand-new one.
BROWSER_PROFILE_PATH = CONFIG_DIR / "browser_profile"
