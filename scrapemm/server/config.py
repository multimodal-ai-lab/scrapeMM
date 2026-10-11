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
    "job_retention_days": float,  # Job history: how long jobs keep their content (0: for ever)
    "max_jobs": int,  # Job history: how many jobs keep their content at most (0: no limit)
    # Known consent platforms' dialogs removed in the browser before reading (default on)
    "remove_consent_dialogs": bool,
    # Limits and lifetimes otherwise fixed in code, each read when used (see `get_setting`)
    "browser_retrieval_timeout": float,
    "archive_retrieval_timeout": float,
    "platform_archive_budget": float,
    "min_media_side": int,
    "max_seconds_per_medium": float,
    "job_max_stored_kb": int,
    "test_runs_kept": int,
    "wayback_hit_ttl": float,
    "wayback_miss_ttl": float,
    "wayback_backoff": float,
    "perma_cc_lookup_hit_ttl": float,
    "perma_cc_lookup_miss_ttl": float,
    "perma_cc_rate_limit_pause": float,
    "perma_cc_retry_budget": float,
    "status_ttl": float,
    "captcha_max_pending": int,
    "max_pdf_pages": int,
    "max_sheet_rows": int,
    "screenshot_max_screens": int,
    "failed_media_memory": float,
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


def get_setting(name: str, default):
    """The setting `name`, or `default` if it is unset or was cleared (stored as None).
    Read at each use, so that a change applies without a restart."""
    value = _config.get(name)
    return default if value is None else value


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
    from .cache import FAILED_MEDIA_MEMORY
    from .challenges import MAX_PENDING
    from .download.documents import MAX_PDF_PAGES, MAX_ROWS_PER_SHEET
    from .download.images import MIN_MEDIA_SIDE
    from .engine import (PLATFORM_ARCHIVE_BUDGET, WAYBACK_HIT_TTL, WAYBACK_MISS_TTL,
                         WAYBACK_BACKOFF)
    from .integrations.headed_browser import BROWSER_RETRIEVAL_TIMEOUT, ARCHIVE_RETRIEVAL_TIMEOUT
    from .integrations.perma_cc import (LOOKUP_HIT_TTL, LOOKUP_MISS_TTL,
                                        LOOKUP_RATE_LIMIT_PAUSE, RETRY_BUDGET)
    from .jobs import MAX_STORED_CONTENT
    from .screenshot import MAX_SCREENS
    from .status import STATUS_TTL
    from .testsuite import MAX_RUNS_KEPT
    from .util import MAX_SECONDS_PER_MEDIUM

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
        entry("max_jobs", DEFAULT_MAX_JOBS, "Jobs with content at most",
              "Older jobs lose their page content (about 110 KB per URL); the jobs and "
              "their statistics are kept. 0 keeps all content.", "history", min=0),
        entry("job_retention_days", float(DEFAULT_RETENTION_DAYS), "Content kept for",
              "Older jobs lose their page content; the jobs are kept. 0 keeps the content "
              "for ever.", "history", unit="days", min=0),
        entry("job_max_stored_kb", MAX_STORED_CONTENT // 1024, "Content stored per field",
              "A job's stored HTML, Markdown or multimodal text is cut beyond this; the UI "
              "says so.", "history", unit="KB", min=16, max=16384),
        entry("test_runs_kept", MAX_RUNS_KEPT, "Test suite runs kept",
              "The oldest test suite reports go first.", "history", min=1, max=500),
        entry("min_media_side", MIN_MEDIA_SIDE, "Min media size",
              "Images and videos smaller than this on either side (icons, logos, avatars) "
              "are left out; 0 keeps all.", "general", unit="px", min=0, max=2048),
        entry("max_seconds_per_medium", float(MAX_SECONDS_PER_MEDIUM), "Time per medium",
              "Longest a single image or video download may take; the page comes back "
              "without it.", "general", unit="s", min=5, max=600),
        entry("max_pdf_pages", MAX_PDF_PAGES, "Max PDF pages",
              "A PDF's text beyond this many pages is left out.", "general", min=1,
              max=100_000),
        entry("max_sheet_rows", MAX_ROWS_PER_SHEET, "Max rows per sheet",
              "A spreadsheet's or CSV file's rows beyond this are left out.", "general",
              min=1, max=1_000_000),
        entry("screenshot_max_screens", MAX_SCREENS, "Screenshot height",
              "How many screens high a page's screenshot reaches at most, from the top.",
              "general", unit="screens", min=1, max=20),
        entry("status_ttl", STATUS_TTL, "Re-check methods every",
              "How long the dashboard reuses a method's status; probes cost real API calls "
              "and log-ins.", "general", unit="s", min=60, max=86400),
        entry("captcha_max_pending", MAX_PENDING, "Max URLs waiting on a CAPTCHA",
              "Per site; further URLs that hit its CAPTCHA are not queued.", "general",
              min=10, max=100_000),
        entry("browser_retrieval_timeout", float(BROWSER_RETRIEVAL_TIMEOUT),
              "Browser retrieval time limit",
              "Longest a single browser retrieval may take before its slot is freed and it "
              "fails.", "browser", unit="s", min=30, max=900),
        entry("archive_retrieval_timeout", float(ARCHIVE_RETRIEVAL_TIMEOUT),
              "Archive replay time limit",
              "Longest a replay by Perma.cc, the Internet Archive or Ghostarchive may take; "
              "their media arrive slowly.", "archives", unit="s", min=60, max=900),
        entry("platform_archive_budget", PLATFORM_ARCHIVE_BUDGET, "Archive time for platforms",
              "Time all archives together get for a social media URL whose integration "
              "failed; 0 skips them.", "archives", unit="s", min=0, max=600),
        entry("wayback_hit_ttl", float(WAYBACK_HIT_TTL), "Wayback lookup: remember a capture",
              "How long a found Internet Archive capture of a URL is reused without asking "
              "again.", "archives", unit="s", min=0, max=86400),
        entry("wayback_miss_ttl", float(WAYBACK_MISS_TTL), "Wayback lookup: remember no capture",
              "How long the answer that a URL has no Internet Archive capture is reused.",
              "archives", unit="s", min=0, max=86400),
        entry("wayback_backoff", float(WAYBACK_BACKOFF), "Wayback lookup: pause on rate limit",
              "How long lookups stop after archive.org rate-limited them twice in a row.",
              "archives", unit="s", min=0, max=3600),
        entry("perma_cc_lookup_hit_ttl", float(LOOKUP_HIT_TTL),
              "Perma.cc lookup: remember a record",
              "How long a found Perma.cc record of a URL is reused without asking again.",
              "archives", unit="s", min=0, max=86400),
        entry("perma_cc_lookup_miss_ttl", float(LOOKUP_MISS_TTL),
              "Perma.cc lookup: remember no record",
              "How long the answer that Perma.cc has no record of a URL is reused.",
              "archives", unit="s", min=0, max=86400),
        entry("perma_cc_rate_limit_pause", float(LOOKUP_RATE_LIMIT_PAUSE),
              "Perma.cc lookup: pause on rate limit",
              "How long lookups stop after Perma.cc rate-limited them.", "archives",
              unit="s", min=0, max=3600),
        entry("perma_cc_retry_budget", float(RETRY_BUDGET), "Perma.cc: time for reloads",
              "How long an empty replay is loaded again before its screenshot is taken "
              "instead.", "archives", unit="s", min=0, max=900),
        entry("failed_media_memory", FAILED_MEDIA_MEMORY, "Remember failed archive media",
              "How long a result missing an archived medium that failed to download is not "
              "kept for good.", "cache", unit="s", min=0, max=86400),
    ]
    return entries  # One per setting, which `test_slots.py` checks


# Load config
_config = load_config()
_apply_config()
