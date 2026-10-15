"""The proxies scrapeMM falls back on when this server's IP address is blocked.

Configured in the web UI (Settings, Proxy): any number of endpoints, each a URL
(`http://`, `https://` or `socks5://` host and port) and whether it is in use, kept in
config.yaml under `proxies`. The username and password are secrets shared by all of them
(`proxy_username`, `proxy_password`; a provider's endpoints usually differ in the port
only), never echoed.

How they are used -- one mechanism, in the engine (`engine._retrieve_single`):

* When a method fails because of this server's address (`ip_blocked()`: YouTube refusing
  the stream or demanding its bot check, HTTP 403/451 answers, rate limits, a region
  block), that method is retried through a proxy, if it can go through one
  (`PROXY_METHODS`). Refused there too, through the next one, up to `proxy_tries` in all.
* When the host refuses connections from this server altogether (the reachability
  verdict "unreachable"), the methods that would fetch from here go through a proxy right
  away instead of being skipped.
* While YouTube is paused for this server's address, YouTube retrievals go through a
  proxy directly (see `integrations.ytdlp._download_youtube`).

Which proxy (`pick()`): each endpoint keeps a record (`proxy_health.json` beside the
config) of its last test -- exit address, country, city, network, latency -- and, per
method, how often it got through and how often it was refused, decaying over days: its
reputation there. An endpoint whose test failed or that failed as a proxy recently is
left out; one refused by a platform rests from that platform's method for a while
(YouTube: as long as YouTube's pause, `youtube_pause`), one refused by a website (the
browser, plain requests) from that website only. Of the rest, the best reputation for the
method wins, the least recently used among equals, so that the load and the risk of
flagging spread across the addresses. A region block (HTTP 451, "not available in your
country") mars no reputation: the next try comes from another region -- for a site that
blocks Europe, from outside Europe. The endpoints are tested again every
`proxy_test_interval`, and with them this server's own location.

An attempt runs with the proxy set as its *route* (a context variable), and every
transport consults it: yt-dlp (`proxy` option), aiohttp (`proxy=` per request; HTTP
proxies only -- with a SOCKS proxy, requests go through curl_cffi instead), curl_cffi
(`proxies=`), and the browser (a separate, short-lived browser context with the proxy, in
the shared browser; see `proxy_browser.py`). Decodo's scraping API fetches through its
own proxies and is left out; so are Firecrawl (its own fetcher) and the archive services.
"""

import asyncio
import contextvars
import json
import logging
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Callable, Iterable, Optional
from urllib.parse import quote, urlsplit

from scrapemm.common.exceptions import (AccessBlockedError, CaptchaEncounteredError,
                                        PaywallError, RateLimitError, RegionBlockedError,
                                        RetrievalFailed)
from scrapemm.common.paths import APP_NAME
from .config import get_config_var, get_setting, update_config
from .paths import CONFIG_DIR
from .secrets import get_secret, remove_secret, set_secret

logger = logging.getLogger(APP_NAME)

CONFIG_KEY = "proxies"
USERNAME_SECRET, PASSWORD_SECRET = "proxy_username", "proxy_password"
SCHEMES = ("http", "https", "socks5", "socks5h")
# A proxy's address: host and port, the scheme optional (see `validate_url()`)
URL_PATTERN = re.compile(r"^((https?|socks5h?)://)?[A-Za-z0-9.\-\[\]:]+:\d{1,5}$", re.IGNORECASE)

# The methods a proxy retry makes sense for: those whose requests go through the
# transports that honour the route (the browser, plain requests, and the integrations
# that download through yt-dlp, aiohttp or curl_cffi)
PROXY_METHODS = {"browser", "Plain HTTP", "YouTube", "TikTok", "Facebook", "Instagram"}

# What an error says when the server's address is what was refused
_IP_MARKERS = ("refused to serve", "http error 403", "http 403", "ip address", "server's region",
               "no bot", "not a bot", "access forbidden", "forbidden", "access denied",
               "http error 451", "http 451", "too many requests", "http error 429",
               # A firewall's "not acceptable" for this server (eci.gov.in: "Request was
               # blocked due to suspicious behavior")
               "http 406")
# What an error says when the proxy itself failed, not the site behind it
_PROXY_FAILURE_MARKERS = ("err_proxy", "err_tunnel", "proxyerror", "proxy error",
                          "proxy connection", "connect to proxy", "tunnel connection failed",
                          "proxy authentication", "http 407", "response 407",
                          "received http code 407")

# Where a test asks for the exit address and its location
ECHO_URL = "https://ipinfo.io/json"

# The methods that fetch any website: a refusal there is the website's, not a platform's
WEB_METHODS = {"browser", "Plain HTTP"}

# Blocked as one region by many sites (e.g. US news sites' HTTP 451 for the GDPR): the
# EU, the EEA and the UK and Switzerland
EUROPE = {"AT", "BE", "BG", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GB",
          "GR", "HR", "HU", "IE", "IS", "IT", "LI", "LT", "LU", "LV", "MT", "NL", "NO",
          "PL", "PT", "RO", "SE", "SI", "SK"}

HEALTH_PATH = CONFIG_DIR / "proxy_health.json"
DEFAULT_TRIES = 3  # Proxies tried for one method of one URL, at most
DEFAULT_REST = 1800.0  # Seconds an endpoint rests from a method that refused it
DEFAULT_TEST_INTERVAL = 3600.0  # Seconds between automatic tests of all endpoints (0: none)
DOWN_FOR = 300.0  # Seconds an endpoint is left out after it failed as a proxy
HALF_LIFE = 3 * 86400.0  # Of the reputation: older outcomes count less and less


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
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""

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
        """The proxy as the browser takes it. An HTTP proxy is always spoken to as
        http://, whichever of http:// and https:// was entered: with https://, every page
        of the browser's proxied contexts failed at once with ERR_PROXY_CONNECTION_FAILED
        (Decodo's proxy, 10-12), while yt-dlp, aiohttp and curl went through it either
        way -- and http:// went through in the browser too. The pages themselves stay
        encrypted end to end; only the proxy's login travels in the clear."""
        server = self.url
        if self.scheme == "https":
            server = "http://" + server.split("://", 1)[1]
        proxy = {"server": server}
        if self.username:
            proxy |= {"username": self.username, "password": self.password or ""}
        return proxy

    @property
    def safe(self) -> str:
        """For logs: the URL, and only whether credentials are set."""
        return self.url + (" (with login)" if self.username else "")


# --- Configuration ------------------------------------------------------------------

def validate_url(url: str) -> str:
    """The proxy's address as stored: scheme://host:port. Without a scheme, an HTTP proxy
    is meant (http://); which of http:// and https:// is entered makes no difference to
    the browser either (see `Proxy.for_playwright()`)."""
    url = (url or "").strip().rstrip("/")
    if not URL_PATTERN.match(url):
        raise ValueError("The proxy URL must look like host:port, optionally with http://, "
                         "https:// or socks5:// in front (credentials go in the fields below).")
    if "://" not in url:
        url = "http://" + url
    scheme, rest = url.split("://", 1)
    url = f"{scheme.lower()}://{rest}"
    port = int(url.rsplit(":", 1)[1])
    if not 1 <= port <= 65535:
        raise ValueError(f"{port} is not a valid port.")
    return url


def _secret(name: str) -> Optional[str]:
    """A secret of the proxies', or None -- also if the secrets store cannot be read: a
    broken store must not fail every retrieval that asks whether there is a proxy."""
    try:
        return get_secret(name) or None
    except RuntimeError as e:
        logger.debug(f"Cannot read the secret {name}: {e}")
        return None


def configured() -> list[Proxy]:
    """The configured endpoints, in use or not, in their order."""
    entries = get_config_var(CONFIG_KEY)
    if not isinstance(entries, list):
        return []
    username, password = _secret(USERNAME_SECRET), _secret(PASSWORD_SECRET)
    return [Proxy(url=e["url"], enabled=bool(e.get("enabled", True)),
                  username=username, password=password)
            for e in entries if isinstance(e, dict) and e.get("url")]


def find(url: str) -> Optional[Proxy]:
    return next((p for p in configured() if p.url == url), None)


def save(entries: list[dict], username: Optional[str] = None, password: Optional[str] = None,
         clear_credentials: bool = False) -> list[Proxy]:
    """Stores the endpoints ({url, enabled} each, in their order). `username`/`password`
    None keeps the stored ones, "" removes them; without endpoints, they go too."""
    stored, seen = [], set()
    for entry in entries:
        url = validate_url(entry.get("url", ""))
        if url in seen:
            raise ValueError(f"{url} is in the list twice.")
        seen.add(url)
        stored.append({"url": url, "enabled": bool(entry.get("enabled", True))})
    update_config(**{CONFIG_KEY: stored})
    if not stored:
        clear_credentials = True
    for secret, value in ((USERNAME_SECRET, username), (PASSWORD_SECRET, password)):
        if clear_credentials or value == "":
            remove_secret(secret)
        elif value is not None:
            set_secret(secret, value)
    _health.prune(seen)
    proxies = configured()
    logger.info(f"Proxies: {len(proxies)} configured, "
                f"{sum(p.enabled for p in proxies)} in use.")
    return proxies


def tries() -> int:
    return max(1, int(get_setting("proxy_tries", DEFAULT_TRIES)))


# --- Health and reputation ----------------------------------------------------------

@dataclass
class Record:
    """An endpoint's track record with one method: its outcomes, decayed (see
    `HALF_LIFE`) as of `at`, and until when it rests from the method after a refusal."""
    ok: float = 0.0
    refused: float = 0.0
    at: float = 0.0
    rest_until: float = 0.0
    last_refusal: str = ""

    def decay(self, now: float) -> None:
        if self.at:
            factor = 0.5 ** (max(0.0, now - self.at) / HALF_LIFE)
            self.ok, self.refused = self.ok * factor, self.refused * factor
        self.at = now

    def score(self, now: float) -> float:
        """The share of attempts that got through, from 0 to 1; 0.5 knowing nothing."""
        factor = 0.5 ** (max(0.0, now - self.at) / HALF_LIFE) if self.at else 1.0
        return (self.ok * factor + 1) / ((self.ok + self.refused) * factor + 2)


@dataclass
class Health:
    test: Optional[dict] = None  # The last test's result (see `test()`)
    down_until: float = 0.0  # Left out until then: it failed as a proxy
    down_reason: str = ""
    methods: dict[str, Record] = field(default_factory=dict)
    # Until when it rests from a website ("<method>@<host>"), refused or region-blocked there
    site_rests: dict[str, float] = field(default_factory=dict)
    day: str = ""
    requests: int = 0  # Attempts through it on `day`
    last_used: float = 0.0

    def record(self, method: str) -> Record:
        return self.methods.setdefault(method, Record())

    def resting(self, method: str, now: float, site: Optional[str] = None) -> bool:
        if site and self.site_rests.get(f"{method}@{site}", 0) > now:
            return True
        record = self.methods.get(method)
        return bool(record and record.rest_until > now)

    def rest_from_site(self, method: str, site: str, until: float, now: float) -> None:
        self.site_rests = {k: t for k, t in self.site_rests.items() if t > now}
        self.site_rests[f"{method}@{site}"] = until

    def reputation(self, method: Optional[str], now: float) -> float:
        """With `method`, with that one; else across all."""
        if method is not None:
            return self.methods.get(method, Record()).score(now)
        total = Record()
        for record in self.methods.values():
            factor = 0.5 ** (max(0.0, now - record.at) / HALF_LIFE) if record.at else 1.0
            total.ok += record.ok * factor
            total.refused += record.refused * factor
        return total.score(now)

    def describe(self, now: float) -> dict:
        today = date.today().isoformat()
        return {
            "test": self.test,
            "down_until": self.down_until if self.down_until > now else None,
            "down_reason": self.down_reason if self.down_until > now else "",
            "reputation": round(self.reputation(None, now), 3),
            "methods": {m: {"reputation": round(r.score(now), 3),
                            "ok": round(r.ok * 0.5 ** ((now - r.at) / HALF_LIFE), 1) if r.at else 0,
                            "refused": round(r.refused * 0.5 ** ((now - r.at) / HALF_LIFE), 1) if r.at else 0,
                            "rest_until": r.rest_until if r.rest_until > now else None,
                            "last_refusal": r.last_refusal}
                        for m, r in sorted(self.methods.items())},
            "site_rests": {k: t for k, t in sorted(self.site_rests.items()) if t > now},
            "requests_today": self.requests if self.day == today else 0,
            "last_used": self.last_used or None,
        }


class _HealthStore:
    """The endpoints' records by URL, kept on disk so that they outlast restarts."""

    SAVE_EVERY = 30.0  # Seconds: routine counts are saved at most this often

    def __init__(self, path=HEALTH_PATH):
        self.path = path
        self._entries: Optional[dict[str, Health]] = None
        self._saved_at = 0.0
        self.last_full_test = 0.0
        self.server_country: Optional[str] = None  # Where this server is, as tested

    @property
    def entries(self) -> dict[str, Health]:
        if self._entries is None:
            self._entries = self._load()
        return self._entries

    def _load(self) -> dict[str, Health]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning(f"Could not read the proxies' records at {self.path}.", exc_info=True)
            return {}
        self.last_full_test = float(data.get("last_full_test") or 0)
        self.server_country = data.get("server_country")
        entries = {}
        for url, raw in (data.get("endpoints") or {}).items():
            try:
                methods = {m: Record(**r) for m, r in (raw.pop("methods", None) or {}).items()}
                entries[url] = Health(**raw, methods=methods)
            except TypeError:
                logger.debug(f"Skipping a malformed proxy record for {url}.")
        return entries

    def get(self, url: str) -> Health:
        return self.entries.setdefault(url, Health())

    def peek(self, url: str) -> Health:
        """Without creating one."""
        return self.entries.get(url) or Health()

    def prune(self, urls: Iterable[str]) -> None:
        keep = set(urls)
        for url in [u for u in self.entries if u not in keep]:
            del self.entries[url]
        self.save(force=True)

    def save(self, force: bool = False) -> None:
        if not force and time.time() - self._saved_at < self.SAVE_EVERY:
            return
        self._saved_at = time.time()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            partial = self.path.with_suffix(".json.part")
            partial.write_text(json.dumps({
                "last_full_test": self.last_full_test,
                "server_country": self.server_country,
                "endpoints": {url: asdict(h) for url, h in self.entries.items()},
            }, indent=1), encoding="utf-8")
            partial.replace(self.path)
        except OSError:
            logger.warning(f"Could not save the proxies' records to {self.path}.", exc_info=True)


_health = _HealthStore()

# Attempts going through each endpoint right now, by URL
_in_flight: dict[str, int] = {}


def in_flight(proxy: Proxy) -> int:
    return _in_flight.get(proxy.url, 0)

# Told the method an endpoint was refused by, e.g. to wake the URLs waiting for its slot
_refusal_listeners: list[Callable[[str], None]] = []


def on_refusal(listener: Callable[[str], None]) -> None:
    _refusal_listeners.append(listener)


def _rest_for(method: str) -> float:
    if method == "YouTube":  # As long as YouTube's own pause: its flags last that long
        from .integrations.ytdlp import youtube_pause
        return youtube_pause()
    return float(get_setting("proxy_rest", DEFAULT_REST))


def region(country: Optional[str]) -> Optional[str]:
    """The region a site blocks `country` as part of: Europe as one, else the country."""
    if not country:
        return None
    return "Europe" if country.upper() in EUROPE else country.upper()


def region_of(proxy: Optional[Proxy]) -> Optional[str]:
    """Where `proxy` exits (as last tested), or with None, where this server is."""
    if proxy is None:
        _health.entries  # Loads the server's country
        return region(_health.server_country)
    return region((_health.peek(proxy.url).test or {}).get("country"))


def usable(proxy: Proxy, method: Optional[str] = None, now: Optional[float] = None,
           site: Optional[str] = None, avoid_regions: Iterable[str] = ()) -> bool:
    """Whether `proxy` may be routed through now: in use, its last test not failed, not
    down, not resting from `method` (on `site`), and not exiting in `avoid_regions`."""
    now = now or time.time()
    health = _health.peek(proxy.url)
    return (proxy.enabled and not (health.test and not health.test.get("ok"))
            and health.down_until <= now
            and not (method and health.resting(method, now, site))
            and not (avoid_regions and region_of(proxy) in set(avoid_regions)))


def pick(method: Optional[str] = None, exclude: Iterable[str] = (), site: Optional[str] = None,
         avoid_regions: Iterable[str] = ()) -> Optional[Proxy]:
    """The endpoint to go through for `method` (to `site`), or None if none is usable (see
    the module's description). `exclude`: the URLs of endpoints tried already;
    `avoid_regions`: where the site turned out to be blocked."""
    now, excluded, avoid = time.time(), set(exclude), set(avoid_regions) - {None}
    candidates = [p for p in configured()
                  if p.url not in excluded and usable(p, method, now, site, avoid)]
    if not candidates:
        return None

    def rank(proxy: Proxy):
        health = _health.peek(proxy.url)
        latency = (health.test or {}).get("latency_ms") or 10 ** 6
        # An idle one first: parallel retrievals spread across the addresses. Then the
        # reputation, in steps of a tenth, so that endpoints about as good take turns.
        return (in_flight(proxy), -round(health.reputation(method, now), 1),
                health.last_used, latency)
    return min(candidates, key=rank)


def usable_count(method: Optional[str] = None) -> int:
    """How many endpoints `method` could go through now."""
    now = time.time()
    return sum(usable(p, method, now) for p in configured())


def idle(method: Optional[str] = None) -> bool:
    """Whether an endpoint usable for `method` has no attempt going through it."""
    now = time.time()
    return any(usable(p, method, now) and not in_flight(p) for p in configured())


def available(method: Optional[str] = None, site: Optional[str] = None) -> bool:
    """Whether a proxy would be picked for `method` (to `site`) now."""
    return pick(method, site=site) is not None


def proxy_failed(error: BaseException) -> bool:
    """Whether an attempt failed because of the proxy itself (unreachable, refusing the
    login), not because of the site behind it."""
    text = f"{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in _PROXY_FAILURE_MARKERS)


def learn(proxy: Proxy, method: str, error: Optional[BaseException],
          site: Optional[str] = None) -> None:
    """Records how an attempt through `proxy` to `site` went: `error` None if it got the
    page."""
    now = time.time()
    health = _health.get(proxy.url)
    if error is None:
        record = health.record(method)
        record.decay(now)
        record.ok += 1
        health.down_until = 0.0
        _health.save()
        return
    if proxy_failed(error):
        health.down_until, health.down_reason = now + DOWN_FOR, str(error).splitlines()[0][:200]
        logger.warning(f"🔌 The proxy {proxy.url} failed ({health.down_reason}); leaving it "
                       f"out for {DOWN_FOR / 60:.0f} min.")
        _health.save(force=True)
        return
    if isinstance(error, RegionBlockedError):
        # Its location, not its reputation: other sites may well serve that region
        if site:
            health.rest_from_site(method, site, now + _rest_for(method), now)
            _health.save(force=True)
        return
    challenged = isinstance(error, CaptchaEncounteredError)
    if not (challenged or ip_blocked(error)):
        return  # The site's doing, whichever address asked: says nothing about the proxy
    record = health.record(method)
    record.decay(now)
    record.refused += 1
    record.last_refusal = str(error).splitlines()[0][:200]
    if not challenged:  # A CAPTCHA mars the reputation, but a human may still solve it
        rest = _rest_for(method)
        if method in WEB_METHODS and site:  # One website's refusal: rest from it only
            health.rest_from_site(method, site, now + rest, now)
            what = site
        else:
            record.rest_until = now + rest
            what = method
        logger.info(f"🔀 {what} refused the proxy {proxy.url}; it rests from {what} for "
                    f"{rest / 60:.0f} min.")
        for listener in _refusal_listeners:
            try:
                listener(method)
            except Exception:
                logger.debug("A proxy refusal listener failed.", exc_info=True)
    _health.save(force=True)


def note_use(proxy: Proxy) -> None:
    health = _health.get(proxy.url)
    today = date.today().isoformat()
    if health.day != today:
        health.day, health.requests = today, 0
    health.requests += 1
    health.last_used = time.time()
    _health.save()


# --- Routing ------------------------------------------------------------------------

@dataclass
class Attempt:
    """One attempt of a method, possibly through a proxy. Transports read its route
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
        if current.used and current.proxy is not None:
            note_use(current.proxy)
            _in_flight[current.proxy.url] = max(0, in_flight(current.proxy) - 1)


def route() -> Optional[Proxy]:
    """The proxy the current attempt goes through, or None to go direct. A transport
    calls this right before a request, so asking counts as using it."""
    current = _attempt.get()
    if current is None or current.proxy is None:
        return None
    if not current.used:
        current.used = True
        _in_flight[current.proxy.url] = in_flight(current.proxy) + 1
    return current.proxy


def routed() -> bool:
    """Whether the current attempt goes through a proxy. Unlike `route()`, asking does
    not count as using it."""
    current = _attempt.get()
    return current is not None and current.proxy is not None


def divert(method: str) -> Optional[Proxy]:
    """Sends the rest of the current attempt through a proxy picked for `method`, if one
    is usable: for a method that knows beforehand that going direct is pointless (YouTube
    while paused for this server's address). The engine then records the attempt as made
    through it."""
    proxy = pick(method)
    current = _attempt.get()
    if proxy is None:
        return None
    if current is None:  # Outside the engine: count it here
        note_use(proxy)
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


def worth_another_route(error: BaseException) -> bool:
    """Whether a method's failure may well not happen through another proxy: its address
    was refused (see `ip_blocked()`), or its route stalled (a video stream that stopped
    coming). A stall says nothing about the proxy's standing, so it costs no reputation."""
    if ip_blocked(error):
        return True
    from scrapemm.server.integrations.ytdlp import STREAM_STALLED
    return isinstance(error, RetrievalFailed) and str(error).startswith(STREAM_STALLED)


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


# --- Status and tests ---------------------------------------------------------------

def describe() -> list[dict]:
    """The endpoints with their records, for the settings."""
    now = time.time()
    return [{"url": p.url, "enabled": p.enabled, "usable": usable(p, now=now),
             **_health.peek(p.url).describe(now)} for p in configured()]


def status() -> dict:
    """What the dashboard shows."""
    proxies, now = configured(), time.time()
    records = [_health.peek(p.url).describe(now) for p in proxies]
    tested = [r["test"] for r in records if r["test"]]
    return {
        "configured": bool(proxies),
        "count": len(proxies),
        "enabled": any(p.enabled for p in proxies),
        "enabled_count": sum(p.enabled for p in proxies),
        "usable_count": sum(usable(p, now=now) for p in proxies),
        "failed_tests": sum(not t.get("ok") for t in tested),
        "countries": sorted({t["country"] for t in tested if t.get("ok") and t.get("country")}),
        "requests_today": sum(r["requests_today"] for r in records),
        "last_used": max((r["last_used"] or 0 for r in records), default=0) or None,
        "last_full_test": _health.last_full_test or None,
        "server_country": _health.server_country,
    }


async def test(proxy: Proxy, timeout: float = 20, keep: bool = True) -> dict:
    """Fetches ECHO_URL through the proxy: the exit address, its location and network,
    and the latency. Through curl_cffi, which speaks HTTP and SOCKS proxies alike. With
    `keep`, the result of a configured endpoint is kept as its health."""
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
                   "region": data.get("region"), "city": data.get("city"),
                   "org": data.get("org"), "latency_ms": round(latency * 1000)}
    except Exception as e:
        message = str(e)
        if "response 407" in message:  # curl fails the CONNECT tunnel itself
            message = "The proxy refused the login (HTTP 407): check the username and password."
        if proxy.password:
            message = message.replace(proxy.password, "***")
        result |= {"ok": False, "error": f"{type(e).__name__}: {message[:300]}",
                   "latency_ms": round((time.monotonic() - started) * 1000)}
    if keep and find(proxy.url) is not None:
        health = _health.get(proxy.url)
        health.test = result
        if result["ok"]:
            health.down_until = 0.0
        _health.save(force=True)
    return result


async def test_all() -> list[dict]:
    """Tests every configured endpoint, a few at a time."""
    gate = asyncio.Semaphore(5)

    async def one(proxy: Proxy) -> dict:
        async with gate:
            return await test(proxy)
    results = await asyncio.gather(*(one(p) for p in configured()), _test_direct())
    results = results[:-1]
    _health.last_full_test = time.time()
    _health.save(force=True)
    if results:
        working = sum(r["ok"] for r in results)
        logger.info(f"Tested the proxies: {working} of {len(results)} work.")
    return list(results)


async def _test_direct() -> None:
    """Where this server itself is: a site that blocks its region is tried from another."""
    from scrapemm.server.download.requests import curl_get
    try:
        response = await curl_get(ECHO_URL, impersonate="chrome124", timeout=20)
        if response.status_code == 200:
            _health.server_country = response.json().get("country") or _health.server_country
    except Exception:
        logger.debug("Could not test this server's own location.", exc_info=True)


async def test_regularly() -> None:
    """Tests all endpoints every `proxy_test_interval` while the server runs: a dead
    endpoint drops out of the routing, one working again comes back."""
    while True:
        await asyncio.sleep(60)
        interval = float(get_setting("proxy_test_interval", DEFAULT_TEST_INTERVAL))
        if interval <= 0 or not configured():
            continue
        _health.entries  # Loads last_full_test
        if time.time() - _health.last_full_test < interval:
            continue
        try:
            await test_all()
        except Exception:
            logger.warning("Could not test the proxies.", exc_info=True)
