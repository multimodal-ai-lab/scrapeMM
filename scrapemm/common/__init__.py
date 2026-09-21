import io
import logging
import os
import sys

import yaml

from .blacklist import (blacklist, blacklist_domain, unblacklist_domain,
                        get_blacklisted_domains, set_blacklist_ttl, DEFAULT_BLACKLIST_TTL)
from .cache import (cache, cache_key, set_cache_ttl, clear_cache, DEFAULT_CACHE_TTL)
from .captcha import detect_captcha
from .exceptions import RateLimitError, RetrievalFailed, CaptchaEncounteredError
from .paths import APP_NAME, CONFIG_DIR, CONFIG_PATH, BLACKLIST_PATH
from .scraping_response import (ScrapingResponse, ScrapedContent,
                                OutputFormat, OUTPUT_FORMATS)

# Set up logger
logger = logging.getLogger(APP_NAME)
logger.setLevel(logging.DEBUG)


def _utf8_stdout():
    """Returns stdout as a UTF-8 stream. Needed because log messages contain emojis
    which consoles with another default encoding (e.g. cp1252 on Windows) cannot print."""
    if (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "") == "utf8":
        return sys.stdout
    try:
        return io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                                line_buffering=True)
    except (AttributeError, ValueError):
        return sys.stdout  # stdout has no underlying binary buffer, e.g. when captured


# Only add handler if none exists (avoid duplicate logs on rerun)
if not logger.hasHandlers():
    handler = logging.StreamHandler(_utf8_stdout())
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
    _apply_config()


def get_config_var(name: str, default=None) -> str:
    return _config.get(name, default)


def _apply_config():
    """Applies the config values that configure runtime behavior."""
    set_cache_ttl(get_config_var("cache_ttl", DEFAULT_CACHE_TTL))
    set_blacklist_ttl(get_config_var("blacklist_ttl", DEFAULT_BLACKLIST_TTL))


def set_wait_on_rate_limit(wait: bool):
    """Set whether to wait on rate limits (particularly relevant for X API).
    Will result in a RateLimitError otherwise."""
    global WAIT_ON_RATE_LIMIT
    WAIT_ON_RATE_LIMIT = wait


# Load config
_config = load_config()
_apply_config()
