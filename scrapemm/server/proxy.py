"""The proxy scrapeMM falls back on when this server's IP address is blocked.

Configured in the web UI (Settings, Proxy): a URL (`http://`, `https://` or `socks5://`
host and port) and whether it is in use, kept in config.yaml under `proxies` -- a list,
so that more than one can be offered later; for now the first entry is the proxy. The
username and password are secrets (`proxy_username`, `proxy_password`), never echoed.

How it is used -- one mechanism, in the engine (`engine._retrieve_single`):

* When a method fails because of this server's address (`ip_blocked()`: YouTube refusing
  the stream or demanding its bot check, HTTP 403/451 answers, rate limits, a region
  block), that method is retried once through the proxy, if one is configured and in use
  and the method can go through it (`PROXY_METHODS`).
* When the host refuses connections from this server altogether (the reachability
  verdict "unreachable"), the methods that would fetch from here go through the proxy
  right away instead of being skipped.
* While YouTube is paused for this server's address, YouTube retrievals go through the
  proxy directly (see `integrations.ytdlp._run_youtube`).

A retry runs with the proxy set as the attempt's *route* (a context variable), and every
transport consults it: yt-dlp (`proxy` option), aiohttp (`proxy=` per request; HTTP
proxies only -- with a SOCKS proxy, requests go through curl_cffi instead), curl_cffi
(`proxies=`), and the browser (a separate, short-lived browser context with the proxy, in
the shared browser; see `proxy_browser.py`). Decodo fetches through its own proxies and
is left out; so are Firecrawl (its own fetcher) and the archive services.
"""

import contextvars
import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Optional
from urllib.parse import quote, urlsplit

from scrapemm.common.exceptions import (AccessBlockedError, CaptchaEncounteredError,
                                        PaywallError, RateLimitError, RegionBlockedError,
                                        RetrievalFailed)
from scrapemm.common.paths import APP_NAME
from .config import get_config_var, update_config
from .secrets import get_secret, remove_secret, set_secret

logger = logging.getLogger(APP_NAME)

CONFIG_KEY = "proxies"
USERNAME_SECRET, PASSWORD_SECRET = "proxy_username", "proxy_password"
SCHEMES = ("http", "https", "socks5", "socks5h")
URL_PATTERN = re.compile(r"^(https?|socks5h?)://[A-Za-z0-9.\-\[\]:]+:\d{1,5}$")

# The methods a proxy retry makes sense for: those whose requests go through the
# transports that honour the route (the browser, plain requests, and the integrations
# that download through yt-dlp, aiohttp or curl_cffi)
PROXY_METHODS = {"browser", "Plain HTTP", "YouTube", "TikTok", "Facebook", "Instagram"}

# What an error says when the server's address is what was refused
_IP_MARKERS = ("refused to serve", "http error 403", "http 403", "ip address", "server's region",
               "no bot", "not a bot", "access forbidden", "forbidden", "access denied",
               "http error 451", "http 451", "too many requests", "http error 429")

# Where "Test proxy" asks for the exit address
ECHO_URL = "https://ipinfo.io/json"


@dataclass
class Proxy:
    url: str  # scheme://host:port, without credentials
    enabled: bool = True
    username: Optional[str] = None
    password: Optional[str] = None

    @property
    def scheme(self) -> str:
        return urlsplit(self.url).scheme

    @property
    def is_socks(self) -> bool:
        return self.scheme.startswith("socks")

    @property
    def with_auth(self) -> str:
        """The URL with the credentials in it, as yt-dlp, aiohttp and curl take it."""
        if not self.username:
            return self.url
        parts = urlsplit(self.url)
        auth = quote(self.username, safe="") + (f":{quote(self.password or '', safe='')}"
                                                if self.password else "")
        return f"{parts.scheme}://{auth}@{parts.netloc}"

    def for_playwright(self) -> dict:
        proxy = {"server": self.url}
        if self.username:
            proxy |= {"username": self.username, "password": self.password or ""}
        return proxy

    @property
    def safe(self) -> str:
        """For logs: the URL, and only whether credentials are set."""
        return self.url + (" (with login)" if self.username else "")


# --- Configuration ------------------------------------------------------------------

def validate_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not URL_PATTERN.match(url):
        raise ValueError("The proxy URL must look like scheme://host:port, with http, https "
                         "or socks5 as the scheme (credentials go in the fields below).")
    port = int(url.rsplit(":", 1)[1])
    if not 1 <= port <= 65535:
        raise ValueError(f"{port} is not a valid port.")
    return url


def _secret(name: str) -> Optional[str]:
    """A secret of the proxy's, or None -- also if the secrets store cannot be read: a
    broken store must not fail every retrieval that asks whether there is a proxy."""
    try:
        return get_secret(name) or None
    except RuntimeError as e:
        logger.debug(f"Cannot read the secret {name}: {e}")
        return None


def configured() -> Optional[Proxy]:
    """The configured proxy (the first entry of `proxies`), in use or not."""
    entries = get_config_var(CONFIG_KEY)
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        return None
    entry = entries[0]
    if not entry.get("url"):
        return None
    return Proxy(url=entry["url"], enabled=bool(entry.get("enabled", True)),
                 username=_secret(USERNAME_SECRET), password=_secret(PASSWORD_SECRET))


def active() -> Optional[Proxy]:
    """The proxy, if one is configured and switched on."""
    proxy = configured()
    return proxy if proxy is not None and proxy.enabled else None


def save(url: Optional[str], enabled: bool, username: Optional[str] = None,
         password: Optional[str] = None, clear_credentials: bool = False) -> Optional[Proxy]:
    """Stores the proxy. `username`/`password` None keeps the stored ones, "" removes them.
    An empty URL removes the proxy altogether."""
    if not (url or "").strip():
        update_config(**{CONFIG_KEY: []})
        clear_credentials = True
    else:
        update_config(**{CONFIG_KEY: [{"url": validate_url(url), "enabled": bool(enabled)}]})
    for secret, value in ((USERNAME_SECRET, username), (PASSWORD_SECRET, password)):
        if clear_credentials or value == "":
            remove_secret(secret)
        elif value is not None:
            set_secret(secret, value)
    proxy = configured()
    logger.info(f"Proxy {'set to ' + proxy.safe + (' (in use)' if proxy.enabled else ' (off)') if proxy else 'removed'}.")
    return proxy


# --- Routing ------------------------------------------------------------------------

@dataclass
class Attempt:
    """One attempt of a method, possibly through the proxy. Transports read its route
    through `route()`, which also records that the proxy was used."""
    proxy: Optional[Proxy]
    used: bool = False


_attempt: contextvars.ContextVar[Optional[Attempt]] = contextvars.ContextVar("proxy_attempt",
                                                                              default=None)


@contextmanager
def attempt(proxy: Optional[Proxy]):
    """Runs the block as one attempt, through `proxy` if given."""
    current = Attempt(proxy)
    token = _attempt.set(current)
    try:
        yield current
    finally:
        _attempt.reset(token)
        if current.used:
            note_use()


def route() -> Optional[Proxy]:
    """The proxy the current attempt goes through, or None to go direct. A transport
    calls this right before a request, so asking counts as using it."""
    current = _attempt.get()
    if current is None or current.proxy is None:
        return None
    current.used = True
    return current.proxy


def divert() -> Optional[Proxy]:
    """Sends the rest of the current attempt through the proxy, if one is in use: for a
    method that knows beforehand that going direct is pointless (YouTube while paused for
    this server's address). The engine then records the attempt as made through it."""
    proxy = active()
    current = _attempt.get()
    if proxy is None:
        return None
    if current is None:  # Outside the engine: count it here
        note_use()
        return proxy
    current.proxy = proxy
    return route()


def aiohttp_proxy() -> Optional[str]:
    """The `proxy=` argument for an aiohttp request, or None. aiohttp speaks HTTP proxies
    only: with a SOCKS proxy, callers go through curl_cffi instead (see `wants_curl()`)."""
    proxy = route()
    if proxy is None or proxy.is_socks:
        return None
    return proxy.with_auth


def wants_curl() -> bool:
    """Whether the current attempt must avoid aiohttp: a SOCKS proxy it cannot use."""
    current = _attempt.get()
    return bool(current and current.proxy and current.proxy.is_socks)


def curl_proxies() -> Optional[dict]:
    """The `proxies=` argument for curl_cffi, or None."""
    proxy = route()
    return {"http": proxy.with_auth, "https": proxy.with_auth} if proxy else None


def ip_blocked(error: BaseException) -> bool:
    """Whether a method failed because of this server's address, so that another address
    may well succeed: a region block, a rate limit or bot check, a refused stream, HTTP
    403/451/429. Not a CAPTCHA (a human solves it in this server's browser) nor a paywall."""
    if isinstance(error, (CaptchaEncounteredError, PaywallError)):
        return False
    if isinstance(error, (RegionBlockedError, RateLimitError)):
        return True
    if isinstance(error, (AccessBlockedError, RetrievalFailed)):
        text = str(error).lower()
        return any(marker in text for marker in _IP_MARKERS)
    return False


def retry_worthy(method: str, error: BaseException) -> bool:
    """Whether to retry `method` through the proxy after it failed with `error`."""
    return method in PROXY_METHODS and active() is not None and ip_blocked(error)


# --- Status -------------------------------------------------------------------------

@dataclass
class _Stats:
    day: str = ""
    requests: int = 0
    last_test: Optional[dict] = None
    last_used: Optional[float] = None


_stats = _Stats()


def note_use() -> None:
    today = date.today().isoformat()
    if _stats.day != today:
        _stats.day, _stats.requests = today, 0
    _stats.requests += 1
    _stats.last_used = time.time()


def status() -> dict:
    """What the dashboard and the settings show."""
    proxy = configured()
    today = date.today().isoformat()
    return {
        "configured": proxy is not None,
        "enabled": bool(proxy and proxy.enabled),
        "url": proxy.url if proxy else None,
        "username_set": bool(proxy and proxy.username),
        "password_set": bool(proxy and proxy.password),
        "requests_today": _stats.requests if _stats.day == today else 0,
        "last_used": _stats.last_used,
        "last_test": _stats.last_test,
    }


async def test(proxy: Proxy, timeout: float = 20) -> dict:
    """Fetches ECHO_URL through the proxy: the exit address, its country and the latency.
    Through curl_cffi, which speaks HTTP and SOCKS proxies alike."""
    from scrapemm.server.download.requests import curl_get
    started = time.monotonic()
    result: dict = {"at": time.time(), "url": proxy.url}
    try:
        response = await curl_get(ECHO_URL, impersonate="chrome124", timeout=timeout,
                                  proxies={"http": proxy.with_auth, "https": proxy.with_auth})
        latency = time.monotonic() - started
        if response.status_code == 407:
            raise RuntimeError("The proxy asks for a login (HTTP 407): check the username "
                               "and password.")
        if response.status_code != 200:
            raise RuntimeError(f"The test address answered HTTP {response.status_code} "
                               f"through the proxy.")
        data = response.json()
        result |= {"ok": True, "ip": data.get("ip"), "country": data.get("country"),
                   "city": data.get("city"), "org": data.get("org"),
                   "latency_ms": round(latency * 1000)}
    except Exception as e:
        message = str(e)
        if "response 407" in message:  # curl fails the CONNECT tunnel itself
            message = "The proxy refused the login (HTTP 407): check the username and password."
        if proxy.password:
            message = message.replace(proxy.password, "***")
        result |= {"ok": False, "error": f"{type(e).__name__}: {message[:300]}",
                   "latency_ms": round((time.monotonic() - started) * 1000)}
    if configured() is not None and configured().url == proxy.url:
        _stats.last_test = result
    return result
