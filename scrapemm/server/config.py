"""The server's persisted configuration (`config.yaml` in the config directory).

Holds everything that is a deployment decision rather than a per-request one: the
Firecrawl endpoints, the hedging delay, cache and blacklist lifetimes. The web UI
edits it through the API; nothing prompts anybody for it.
"""

import logging
import os
from typing import Any

import yaml

from scrapemm.common.paths import APP_NAME
from .paths import CONFIG_PATH
from .blacklist import set_blacklist_ttl, DEFAULT_BLACKLIST_TTL
from .cache import (cache, immutable, DEFAULT_CACHE_TTL, DEFAULT_MAX_ENTRIES, DEFAULT_MAX_MB,
                    DEFAULT_IMMUTABLE_MAX_MB)

logger = logging.getLogger(APP_NAME)

# The settings the API exposes, with the type each value is coerced to. Anything not
# listed here is rejected, so a typo in the UI cannot quietly create a dead setting.
SETTINGS: dict[str, type] = {
    "firecrawl_url": str,
    "firecrawl_urls": list,
    "hedging_delay": float,
    "cache_ttl": float,
    "cache_enabled": bool,
    "cache_max_entries": int,
    "cache_max_mb": float,
    "cache_immutable_max_mb": float,  # The permanent tier, on disk
    "blacklist_ttl": float,
    "archive_today_interactive_solve": bool,
    "archive_today_screenshot_fallback": bool,
    "archive_today_solve_timeout": float,
    "devtools_local_port": int,
    "browser_user_agent": str,
    "max_concurrency": int,
    "max_browser_pages": int,
    "max_search_concurrency": int,
    "disabled_methods": list,
    "youtube_min_interval": float,
    "youtube_cooldown": float,
    "job_retention_days": float,  # Job history: how long jobs are kept (0: for ever)
    "max_jobs": int,  # Job history: how many jobs are kept at most (0: no limit)
    # Known consent platforms' dialogs removed in the browser before reading (default on)
    "remove_consent_dialogs": bool,
}


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def update_config(**kwargs):
    _config.update(kwargs)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(_config, f)
    _apply_config()


def get_config_var(name: str, default=None) -> Any:
    return _config.get(name, default)


def get_config() -> dict:
    """The full configuration, as the API serves it."""
    return dict(_config)


def _or_default(value, default):
    return default if value is None else value


def _apply_config():
    """Applies the config values that configure runtime behavior."""
    cache.configure(enabled=get_config_var("cache_enabled", True),
                    ttl=_or_default(get_config_var("cache_ttl"), DEFAULT_CACHE_TTL),
                    max_entries=_or_default(get_config_var("cache_max_entries"), DEFAULT_MAX_ENTRIES),
                    max_mb=_or_default(get_config_var("cache_max_mb"), DEFAULT_MAX_MB))
    immutable.configure(max_mb=_or_default(get_config_var("cache_immutable_max_mb"),
                                           DEFAULT_IMMUTABLE_MAX_MB))
    set_blacklist_ttl(get_config_var("blacklist_ttl", DEFAULT_BLACKLIST_TTL))


WAIT_ON_RATE_LIMIT = False


def set_wait_on_rate_limit(wait: bool):
    """Set whether to wait on rate limits (particularly relevant for X API).
    Will result in a RateLimitError otherwise."""
    global WAIT_ON_RATE_LIMIT
    WAIT_ON_RATE_LIMIT = wait


def _seed_from_environment():
    """Lets the container's .env prime the configuration on a fresh volume. Values
    already in config.yaml win: the UI is the authority once someone has used it."""
    seeded = {}
    if (limit := os.getenv("SCRAPEMM_MAX_CONCURRENCY")) and not _config.get("max_concurrency"):
        try:
            seeded["max_concurrency"] = int(limit)
        except ValueError:
            logger.warning(f"Ignoring invalid SCRAPEMM_MAX_CONCURRENCY={limit!r}.")
    if seeded:
        update_config(**seeded)


# Load config
_config = load_config()
_apply_config()
_seed_from_environment()
