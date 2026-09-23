"""What the dashboard shows: whether each retrieval method can be used right now.

`RetrievalIntegration.connected` already exists, but it is set lazily on the first
retrieval, which is no good for a dashboard that should answer the question *before*
anybody scrapes anything. So this module drives the connection deliberately, records
why it failed if it did, and names the secrets that are missing -- the dashboard's job
is to turn "Instagram doesn't work" into "Instagram needs instagram_cookie".

Firecrawl and Decodo are not integrations (they are general scraping methods), but from
a dashboard's point of view they are the same kind of thing: something that either works
or needs configuring. They get cards here too.

Statuses are cached, because connecting costs real API calls and the UI polls.
"""

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from scrapemm.common.exceptions import CaptchaEncounteredError
from scrapemm.common.paths import APP_NAME
from . import registry
from .blacklist import blacklist
from .cache import cache
from .environment import ffmpeg_available, ffprobe_available
from .jobs import jobs
from .secrets import is_set
from .toggles import is_enabled

logger = logging.getLogger(APP_NAME)

# How long a probe result is served before it is taken again
STATUS_TTL = 60.0

# How long one method may take to answer before the dashboard gives up on it
PROBE_TIMEOUT = 20.0

# How many recent retrievals the success rate is computed over. Recent rather than
# all-time: months of history would average away the degradation it exists to show.
RECENT_WINDOW = 1000

# Which secrets each method needs before it can work at all. Kept here rather than on
# the integrations so that the dashboard can explain a failure without having to run the
# probe first.
#
# Keyed by `RetrievalIntegration.name` in lower case -- the same key `NAME_TO_INTEGRATION`
# uses -- so "x (twitter)" rather than "x". `test_status.py` asserts that every key here
# names something that actually exists, because a typo would silently turn into "this
# needs no secrets" and the dashboard would report a confusing "Could not connect"
# instead of "you have not configured it".
REQUIRED_SECRETS: dict[str, tuple[str, ...]] = {
    "x (twitter)": ("x_bearer_token",),
    "telegram": ("telegram_api_id", "telegram_api_hash", "telegram_bot_token"),
    "bluesky": ("bluesky_username", "bluesky_password"),
    "tiktok": ("tiktok_client_key", "tiktok_client_secret"),
    "reddit": ("reddit_client_id", "reddit_client_secret"),
    "decodo": ("decodo_token",),
}

# Secrets that widen what an integration can reach without being needed to use it at
# all. Facebook and Instagram serve most public content to anyone; the cookie is what
# unlocks login-gated and age-restricted posts. Missing one is worth flagging, but as a
# limitation rather than as "this does not work".
OPTIONAL_SECRETS: dict[str, tuple[str, ...]] = {
    "facebook": ("facebook_cookie",),
    "instagram": ("instagram_cookie",),
}

# The general scraping methods, which are not `RetrievalIntegration`s
METHOD_KEYS = ("firecrawl", "decodo")

# The states a card can be in, which is also the colour it gets in the UI
READY, UNCONFIGURED, ERROR, DISABLED = "ready", "unconfigured", "error", "disabled"
# Works, but not to its full extent -- an optional credential is missing
LIMITED = "limited"
# Behind a CAPTCHA at this moment. Distinct from ERROR because nothing is broken and
# nothing needs fixing: a human solving one check clears it.
GATED = "gated"


@dataclass
class IntegrationStatus:
    name: str
    key: str = ""  # Stable lower-case id, used by the API and the UI
    kind: str = "integration"  # "integration" or "method"
    domains: list[str] = field(default_factory=list)
    required_secrets: list[str] = field(default_factory=list)
    missing_secrets: list[str] = field(default_factory=list)
    optional_secrets: list[str] = field(default_factory=list)
    missing_optional_secrets: list[str] = field(default_factory=list)
    configured: bool = False
    connected: Optional[bool] = None
    retrievals: int = 0  # URLs this method has successfully retrieved
    configurable: bool = False  # Whether any secret of its own can be set
    gated: bool = False  # Behind a CAPTCHA at this moment
    enabled: bool = True
    state: str = ERROR
    detail: str = ""
    checked_at: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


_cache: dict[str, tuple[float, IntegrationStatus]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(name: str) -> asyncio.Lock:
    """One probe at a time per method, so a burst of dashboard polls does not open five
    Telegram sessions at once."""
    if name not in _locks:
        _locks[name] = asyncio.Lock()
    return _locks[name]


def all_keys() -> list[str]:
    """Every method the dashboard shows, integrations first."""
    return [method["key"] for method in all_methods()]


def all_methods() -> list[dict]:
    """Every method the dashboard shows, with the name to label it by.

    The name comes along so that a UI laying out its cards before the probes finish can
    show what each one *is* straight away, instead of a placeholder that fills in later.
    """
    from .integrations import RETRIEVAL_INTEGRATIONS

    methods = [{"key": i.name.lower(), "name": i.name, "kind": "integration",
                "domains": list(i.domains)}
               for i in RETRIEVAL_INTEGRATIONS]
    methods += [{"key": "firecrawl", "name": "Firecrawl", "kind": "method",
                 "domains": []},
                {"key": "decodo", "name": "Decodo", "kind": "method", "domains": []}]
    return methods


async def check(name: str, force: bool = False) -> IntegrationStatus:
    """Probes one method, serving a recent result unless `force` says otherwise."""
    from .integrations import NAME_TO_INTEGRATION

    key = name.lower()
    if key not in NAME_TO_INTEGRATION and key not in METHOD_KEYS:
        raise KeyError(f"Unknown integration or method '{name}'.")

    if not force:
        cached = _cache.get(key)
        if cached and time.time() - cached[0] < STATUS_TTL:
            return cached[1]

    async with _lock_for(key):
        # Another caller may have probed while we waited for the lock
        cached = _cache.get(key)
        if not force and cached and time.time() - cached[0] < STATUS_TTL:
            return cached[1]

        if key in METHOD_KEYS:
            status = await _probe_method(key)
        else:
            status = await _probe(key, NAME_TO_INTEGRATION[key])
        _cache[key] = (time.time(), status)
        return status


_counts_cache: tuple[float, dict[str, int]] = (0.0, {})


def _retrieval_counts() -> dict[str, int]:
    """How many URLs each method has retrieved, keyed in lower case.

    Cached alongside the statuses: it is one aggregate over the whole results table, and
    every card asks for it.
    """
    global _counts_cache
    cached_at, counts = _counts_cache
    if time.time() - cached_at < STATUS_TTL:
        return counts
    counts = {method.lower(): n for method, n in jobs.method_counts().items()}
    _counts_cache = (time.time(), counts)
    return counts


def _base_status(key: str, name: str, kind: str,
                 domains: list[str]) -> IntegrationStatus:
    required = list(REQUIRED_SECRETS.get(key, ()))
    optional = list(OPTIONAL_SECRETS.get(key, ()))
    return IntegrationStatus(
        name=name,
        key=key,
        kind=kind,
        domains=domains,
        required_secrets=required,
        optional_secrets=optional,
        missing_secrets=[s for s in required if not is_set(s)],
        missing_optional_secrets=[s for s in optional if not is_set(s)],
        configured=all(is_set(s) for s in required),
        enabled=is_enabled(key),
        # The Headed Browser, the archives and the like take no credentials at all;
        # offering to configure them would lead somewhere with nothing to fill in.
        configurable=bool(required or optional),
        retrievals=_retrieval_counts().get(name.lower(), 0),
        checked_at=time.time(),
    )


def _settle(status: IntegrationStatus) -> IntegrationStatus:
    """Works out the card's final state. Disabled wins over everything else: a method
    nobody is going to call should not be reported as broken."""
    if not status.enabled:
        status.state = DISABLED
        status.detail = "Disabled. scrapeMM will not use it."
    elif status.missing_secrets:
        status.state = UNCONFIGURED
    elif status.gated:
        status.state = GATED
    elif not status.connected:
        status.state = ERROR
    elif status.missing_optional_secrets:
        status.state = LIMITED
        status.detail = ("Works for public content. The cookie unlocks login-gated and "
                         "age-restricted posts.")
    else:
        status.state = READY
    return status


async def _probe(key: str, integration) -> IntegrationStatus:
    status = _base_status(key, integration.name, "integration",
                          list(integration.domains))

    if not status.enabled or status.missing_secrets:
        # Neither case needs the network, and probing a method that is switched off or
        # has no credentials would only produce a misleading error.
        status.connected = False
        if status.missing_secrets:
            status.detail = f"Missing {len(status.missing_secrets)} secret(s)."
        return _settle(status)

    try:
        # A live probe rather than a reading of whatever the last retrieval happened to
        # leave behind. Bounded, because an unreachable service can otherwise hold the
        # whole dashboard hostage for its own timeout.
        await asyncio.wait_for(integration.probe(), timeout=PROBE_TIMEOUT)
        status.connected = bool(integration.connected)
        status.detail = "Connected." if status.connected else "Could not connect."
    except asyncio.TimeoutError:
        integration.connected = False
        status.connected = False
        status.detail = f"Did not answer within {PROBE_TIMEOUT:.0f}s."
    except CaptchaEncounteredError as e:
        # Not a fault: the service is up, a human simply has to pass a check.
        integration.connected = False
        status.connected = False
        status.gated = True
        status.detail = str(e)
    except BaseException as e:
        # BaseException rather than Exception: a probe may be cancelled from inside
        # (the browser integrations do), and one method's cancellation must not take
        # down the dashboard request that asked about all of them.
        integration.connected = False
        status.connected = False
        status.detail = f"{type(e).__name__}: {e}"
        logger.debug(f"Probe of {integration.name} failed.", exc_info=True)
        if isinstance(e, KeyboardInterrupt | SystemExit):
            raise

    return _settle(status)


async def _probe_method(key: str) -> IntegrationStatus:
    """Firecrawl and Decodo: scraping methods rather than per-platform integrations,
    but the dashboard treats them the same way."""
    if key == "firecrawl":
        from .integrations.firecrawl.firecrawl import (configured_firecrawl_urls,
                                                       find_all_firecrawls)

        status = _base_status(key, "Firecrawl", "method", [])
        if not status.enabled:
            status.connected = False
            return _settle(status)

        configured = configured_firecrawl_urls()
        try:
            reachable = await asyncio.wait_for(find_all_firecrawls(configured),
                                               timeout=PROBE_TIMEOUT)
        except Exception:
            logger.debug("Probing the Firecrawl instances failed.", exc_info=True)
            reachable = []
        status.connected = bool(reachable)
        status.domains = configured
        status.detail = (f"{len(reachable)} of {len(configured)} instance(s) reachable."
                         if configured else "No instance configured.")
        return _settle(status)

    # Decodo: a token is all it takes; there is no free endpoint to ping without
    # spending a billable request, so a configured token counts as ready.
    status = _base_status(key, "Decodo", "method", [])
    status.connected = status.configured and status.enabled
    if status.enabled and status.configured:
        status.detail = "Token configured."
    return _settle(status)


async def check_all(force: bool = False) -> list[IntegrationStatus]:
    """Probes everything concurrently."""
    keys = all_keys()
    results = await asyncio.gather(*(_check_safely(key, force) for key in keys),
                                   return_exceptions=True)
    statuses = []
    for key, result in zip(keys, results):
        if isinstance(result, BaseException):
            statuses.append(_settle(IntegrationStatus(
                name=key, key=key, connected=False, enabled=is_enabled(key),
                detail=f"{type(result).__name__}: {result}", checked_at=time.time())))
        else:
            statuses.append(result)
    return statuses


async def _check_safely(key: str, force: bool):
    """`asyncio.gather(return_exceptions=True)` does not absorb a CancelledError raised
    by a child, so it is absorbed here -- one method cancelling its own probe must not
    fail the whole dashboard."""
    try:
        return await check(key, force)
    except asyncio.CancelledError:
        return RuntimeError("The probe was cancelled.")


def invalidate(*names: str) -> None:
    """Forgets cached statuses, so the next poll probes afresh. Called when a secret
    changes: the dashboard should reflect that immediately, not a minute later."""
    global _counts_cache
    _counts_cache = (0.0, {})
    if names:
        for name in names:
            _cache.pop(name.lower(), None)
    else:
        _cache.clear()


def secrets_to_integrations(secret_name: str) -> list[str]:
    """Which methods a given secret affects, required or optional."""
    return sorted({name for mapping in (REQUIRED_SECRETS, OPTIONAL_SECRETS)
                   for name, secrets in mapping.items() if secret_name in secrets})


async def environment() -> dict:
    """The health of everything that is not a retrieval method."""
    return {
        "ffmpeg": {
            "available": ffmpeg_available,
            "detail": "Videos are merged and normalized." if ffmpeg_available
                      else "Missing: videos download without sound and are not normalized.",
        },
        "ffprobe": {
            "available": ffprobe_available,
            "detail": "Available." if ffprobe_available
                      else "Missing: videos are not checked for browser playability.",
        },
        "playwright": await _playwright_status(),
        "display": _display_status(),
        "captcha": _captcha_backlog(),
        "media": registry.usage(),
        "address": _address(),
        "throughput": {
            "last_24h": jobs.count_since(24 * 60 * 60),
            "success_rate": jobs.recent_success_rate(RECENT_WINDOW),
        },
        "blacklist": {
            "domains": len(blacklist.domains()),
            "ttl": blacklist.ttl,
        },
        "cache": {
            "entries": len(cache),
            "ttl": cache.ttl,
        },
        "jobs": jobs.stats(),
    }


_playwright_cache: Optional[dict] = None


async def _playwright_status() -> dict:
    """Whether Playwright's Chromium is actually installed. A pip install of Playwright
    does not bring the browser, and everything that needs a real browser fails
    confusingly without it.

    Computed once and kept: answering it starts Playwright's driver process, which costs
    about a second, and nobody installs a browser into a running container.
    """
    global _playwright_cache
    if _playwright_cache is not None:
        return _playwright_cache

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            path = p.chromium.executable_path
        available = bool(path) and os.path.exists(path)
        _playwright_cache = {
            "available": available,
            "detail": path if available
            else "Chromium is not installed. Run `playwright install chromium`.",
        }
    except Exception as e:
        _playwright_cache = {"available": False, "detail": f"{type(e).__name__}: {e}"}
    return _playwright_cache


def _address() -> dict:
    """Where this server can be reached.

    Inside a container the interface addresses are the bridge network's (172.17.x.x),
    which are of no use to anybody outside it -- so the honest answer is the port plus
    whatever the deployment declares as its public URL, and the UI falls back to the
    origin the browser is already using.
    """
    import socket

    addresses = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in addresses and not address.startswith("127."):
                addresses.append(address)
    except OSError:
        pass

    return {
        "port": int(os.getenv("SCRAPEMM_PORT", "8080")),
        "public_url": os.getenv("SCRAPEMM_PUBLIC_URL") or None,
        "addresses": addresses,
        # True when those addresses are container-internal and should not be offered
        # as something to hand to a colleague.
        "containerised": os.path.exists("/.dockerenv"),
    }


def _captcha_backlog() -> dict:
    """How much work is waiting on somebody solving a CAPTCHA.

    Archive.today buffers every gated snapshot rather than blocking on a human, so this
    is the number that says whether anyone needs to go and do something.
    """
    try:
        from .integrations.archive_today import (count_cached_archive_today_pages,
                                                 get_archive_today_buffer)
        waiting = len(get_archive_today_buffer())
        cached = count_cached_archive_today_pages()
    except Exception:
        logger.debug("Could not read the Archive.today backlog.", exc_info=True)
        waiting, cached = 0, 0

    return {
        "waiting": waiting,
        "cached_pages": cached,
        # Without a display there is no way to solve one, which is worth saying when
        # something is actually waiting.
        "solvable": bool(os.getenv("DISPLAY")) or os.name == "nt",
    }


def _display_status() -> dict:
    """Whether a display is around for the headed browser the CAPTCHA panel drives."""
    display = os.getenv("DISPLAY")
    vnc = shutil.which("x11vnc") is not None
    return {
        "display": display,
        "headful_possible": bool(display) or os.name == "nt",
        "x11vnc": vnc,
        "detail": ("Ready for the CAPTCHA panel." if display and vnc
                   else "No DISPLAY/x11vnc: the CAPTCHA panel is unavailable."),
    }
