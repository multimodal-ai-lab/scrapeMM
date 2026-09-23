"""Where the scrapeMM server keeps its state.

Everything lives inside one config directory, which the Docker image points at a
mounted volume so that secrets, caches, the job history and the browser profile all
survive a container rebuild.
"""

import os
from pathlib import Path

from platformdirs import user_config_dir

from scrapemm.common.paths import APP_NAME

CONFIG_DIR = Path(os.getenv("SCRAPEMM_CONFIG_DIR") or user_config_dir(APP_NAME))
os.makedirs(CONFIG_DIR, exist_ok=True)

CONFIG_PATH = CONFIG_DIR / "config.yaml"
BLACKLIST_PATH = CONFIG_DIR / "blacklist.yaml"

# The symmetric key that encrypts the secrets store. Generated on first start; the
# admin never handles it (see `scrapemm.server.secrets`).
MASTER_KEY_PATH = CONFIG_DIR / "master.key"
SECRETS_PATH = CONFIG_DIR / "secrets"

# Job history: parameters, per-URL outcomes and the content that was retrieved
JOBS_DB_PATH = CONFIG_DIR / "jobs.db"

# Profile of the shared headed browser. Keeping it across runs is what makes a session
# that passed a bot check last: the cookies the server refreshes on every response stay
# in the profile, and the browser keeps looking like the same returning client instead
# of a brand-new one.
BROWSER_PROFILE_PATH = CONFIG_DIR / "browser_profile"

# Telethon's session database. Inside the config directory, so it survives a container
# rebuild -- a Telegram session is worth keeping -- and so it does not depend on the
# working directory, which used to leave it unwritable on a server.
TELEGRAM_SESSION_PATH = CONFIG_DIR / "telegram"

# Archive.today's permanent caches and the backlog waiting for the next solved CAPTCHA
SNAPSHOT_CACHE_PATH = CONFIG_DIR / "archive_today_snapshots.json"
PAGE_CACHE_DIR = CONFIG_DIR / "archive_today_pages"
BUFFER_PATH = CONFIG_DIR / "archive_today_buffer.json"
