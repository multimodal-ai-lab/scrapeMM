"""The server's persisted configuration (`config.yaml` in the config directory).

Holds everything that is a deployment decision rather than a per-request one: the
Firecrawl endpoints, the hedging delay, cache and blacklist lifetimes, each method's
slots. The web UI edits it through the API (described to it by `schema()`); nothing
prompts anybody for it.
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
    # Per retrieval method (see `slots`): {method: slots}, {method: seconds}. Only the
    # overrides of the defaults are stored.
    "method_slots": dict,
    "method_cooldowns": dict,
    "max_browser_pages": int,
    "max_search_concurrency": int,
    "disabled_methods": list,
    # Seconds YouTube is left alone after it refused this server. Read in its place
    # if unset: `youtube_cooldown`, its former name. And `youtube_min_interval`, the
    # former YouTube pace, stands in for the "youtube" entry of `method_cooldowns`.
    "youtube_pause": float,
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


def schema() -> list[dict]:
    """What the UI needs to know to edit each setting in SETTINGS: its type, its default
    (the one the code uses when the setting is unset), a label and a one-sentence
    description, the settings page section it belongs to, the unit the value is stored
    in, and its bounds. A function rather than a constant: the defaults live in modules
    that import this one."""
    from . import slots
    from .engine import DEFAULT_HEDGING_DELAY
    from .integrations.archive_today import DEFAULT_SOLVE_TIMEOUT
    from .integrations.headed_browser import DEFAULT_DEVTOOLS_LOCAL_PORT, DEFAULT_MAX_BROWSER_PAGES
    from .integrations.ytdlp import DEFAULT_YOUTUBE_PAUSE
    from .jobs import DEFAULT_MAX_JOBS, DEFAULT_RETENTION_DAYS
    from .search.base import DEFAULT_MAX_SEARCH_CONCURRENCY

    def entry(key: str, default, label: str, description: str, section: str,
              **extra) -> dict:
        return {"key": key, "type": SETTINGS[key].__name__, "default": default,
                "label": label, "description": description, "section": section, **extra}

    method_keys = slots.keys()
    entries = [
        entry("firecrawl_urls", [], "Firecrawl instances",
              "scrapeMM spreads its scrapes across every instance that responds.", "general"),
        entry("firecrawl_url", None, "Firecrawl instance (former single one)",
              "A single Firecrawl instance, as configured before several could be; used "
              "alongside the list.", "general"),
        entry("hedging_delay", DEFAULT_HEDGING_DELAY, "Hedging delay",
              "Head start of each retrieval method before the next one runs alongside it; "
              "empty or 0 runs them one after another.", "general", unit="s", min=0),
        entry("blacklist_ttl", DEFAULT_BLACKLIST_TTL, "Blacklist lifetime",
              "How long automatic CAPTCHA blacklistings last; 0 never expires.", "general",
              unit="s", min=0),
        entry("disabled_methods", [], "Disabled methods",
              "The retrieval methods and integrations switched off.", "chain"),
        entry("method_slots", {k: slots.default_slots(k) for k in method_keys},
              "Slots per method",
              "How many retrievals each method runs at once; more wait for a free slot.",
              "parallelism", min=1),
        entry("method_cooldowns", {k: slots.default_cooldown(k) for k in method_keys},
              "Cooldown per method",
              "Seconds between the starts of two retrievals by the method (plus up to 50% "
              "jitter); 0 for none.", "parallelism", unit="s", min=0),
        entry("max_browser_pages", DEFAULT_MAX_BROWSER_PAGES, "Max concurrent browser pages",
              "Pages open in the server's browser at once (the Browser method, the "
              "archives); lower it if the server runs short of memory.", "parallelism", min=1),
        entry("max_search_concurrency", DEFAULT_MAX_SEARCH_CONCURRENCY,
              "Max concurrent searches",
              "Searches with a search provider at once, across all clients; keeps bursts "
              "within the provider's rate limit.", "parallelism", min=1),
        entry("browser_user_agent", None, "Browser user agent",
              "The user agent the server's browser presents; empty for Chromium's own.",
              "browser"),
        entry("remove_consent_dialogs", True, "Remove cookie/consent dialogs",
              "Removes the dialogs of known consent platforms in the browser before the "
              "page is read.", "browser"),
        entry("devtools_local_port", DEFAULT_DEVTOOLS_LOCAL_PORT, "DevTools port",
              "The local port of the browser's remote debugging interface.", "browser",
              min=1, max=65535),
        entry("youtube_pause", DEFAULT_YOUTUBE_PAUSE, "YouTube pause after a refusal",
              "How long YouTube is left alone once it refused this server; with a proxy in "
              "use, YouTube goes through it meanwhile.", "integrations", unit="s", min=0),
        entry("archive_today_interactive_solve", False,
              "Archive.today: ask for a CAPTCHA at the moment of a gated request",
              "Sensible only for one-off, attended retrievals.", "archives"),
        entry("archive_today_screenshot_fallback", False,
              "Archive.today: serve the snapshot's screenshot when it is gated",
              "Gives an unattended run something rather than an error, but a screenshot is "
              "not the page text.", "archives"),
        entry("archive_today_solve_timeout", float(DEFAULT_SOLVE_TIMEOUT),
              "Archive.today: CAPTCHA solving time",
              "How long a gated Archive.today request waits for somebody to solve its "
              "CAPTCHA.", "archives", unit="s", min=0),
        entry("cache_enabled", True, "Re-use recent results",
              "Serves a recent successful retrieval of the same URL instead of scraping it "
              "again.", "cache"),
        entry("cache_ttl", float(DEFAULT_CACHE_TTL), "Cache lifetime",
              "How long a result is re-used; 0 turns the cache off too.", "cache",
              unit="s", min=0),
        entry("cache_max_entries", DEFAULT_MAX_ENTRIES, "Max cache entries",
              "The oldest go first.", "cache", min=1, max=1_000_000),
        entry("cache_max_mb", float(DEFAULT_MAX_MB), "Max cache size",
              "Page text in memory; media stay on disk.", "cache", unit="MB", min=1,
              max=64 * 1024),
        entry("cache_immutable_max_mb", float(DEFAULT_IMMUTABLE_MAX_MB),
              "Permanent tier size", "The permanent cache tier on disk (archive captures), "
              "besides the media registry.", "cache", unit="MB", min=1, max=1024 * 1024),
        entry("max_jobs", DEFAULT_MAX_JOBS, "Jobs kept at most",
              "The oldest go first; 0 keeps all.", "history", min=0),
        entry("job_retention_days", float(DEFAULT_RETENTION_DAYS), "Jobs kept for",
              "Older jobs and search records are removed; 0 keeps them for ever.",
              "history", unit="days", min=0),
    ]
    return entries  # One per setting, which `test_slots.py` checks


# Load config
_config = load_config()
_apply_config()
