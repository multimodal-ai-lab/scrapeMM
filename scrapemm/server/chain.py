"""The retrieval chain: which methods scrapeMM tries for a URL, and in which order.

This is the one place that decides it. The engine executes what `resolve()` returns
and nothing else; the web UI edits the configured chain through the admin API.

The model
---------
The chain is one ordered list of *steps*, each a general retrieval method, and each
either a *live* method (it fetches the page as it is now) or an *archive* method (it
gets a copy an archive captured):

    integrations -> browser -> firecrawl -> decodo -> plain_http -> perma_cc -> wayback
    (live .......................................................)  (archive ........)

The order is the order the engine follows, exactly as given: archive methods may come
before live ones (to prefer an archived copy, say), though by default they come last,
because an archived copy may be years old while the live page is current. Whether a step
is used at all is configurable too.

*Exceptions* (`retrieval_exceptions`, the former domain routes) fix the live methods for
particular domains. See below.

Semantics, as `resolve()` and the engine apply them
---------------------------------------------------
* **Platform integrations come first for their domains.** The `integrations` step stands
  for "the site's own integration, if it has one" (X, TikTok, Telegram, YouTube, the
  archive services, ...). For a domain without one it is skipped.
* **Exceptions replace the live methods.** Some domains are known to work only (or best)
  with particular methods: social platforms with their integration alone,
  washingtonpost.com with Decodo, and so on. An exception names a domain pattern
  ("example.com" for the domain and its subdomains, "*.example.com" for its subdomains
  only; the most specific one wins) and the live methods to use. They take the place of
  the chain's live methods, at the position of its first one; the chain's archive methods
  keep theirs. Steps switched off in the chain stay off there too. Exceptions list live
  methods only.
* **Plain HTTP is for the open web.** It is skipped for domains that have an integration
  (a plain GET of a tweet or a TikTok page yields a login wall, never the content).
* **Archived copies of platform URLs must carry the platform's content.** For a domain
  with its own integration, an archived copy counts only with at least one image or video
  -- a video, for video URLs (`VIDEO_URL`) -- and not if it shows a login or consent page:
  a captured Facebook login wall is no post. Otherwise the copy is rejected and the
  integration's own verdict (unavailable, blocked, ...) stands. The archive methods of a
  platform URL also share a time budget (`engine.PLATFORM_ARCHIVE_BUDGET`), so that they
  add seconds to a URL the platform already refused, not minutes.
* **No archives for platforms they do not preserve.** The archive methods are skipped
  for the platforms in `ARCHIVES_USELESS_FOR`: neither the Wayback Machine nor Perma.cc
  keeps YouTube's videos (their captures hold the player page, thumbnails and comments),
  so looking there only delays the platform's own verdict.
* **Archive URLs are not looked up in archives.** For a URL of an archive service itself
  (web.archive.org, perma.cc, archive.today, ...) the archive methods are skipped.
* **Outcomes.** The first method whose result passes the checks (not empty, no CAPTCHA,
  no paywall teaser, in the requested format) wins. A CAPTCHA, a paywall teaser or an
  error moves on to the next step. A page reported missing (HTTP 404/410) or a host that
  is down skips the remaining live methods -- they would find the same -- while the
  archive methods still run. So does a domain behind a CAPTCHA that already waits for a
  human. A host that refuses this server skips the methods fetching from here (browser,
  Firecrawl when self-hosted, plain HTTP).
* **Hedging** (`hedging_delay`, off by default) applies to consecutive live methods: each
  gets that head start before the next one runs alongside it. Archive methods run
  strictly one after another.
* **Explicit method lists** from a client bypass the chain and the exceptions: exactly
  the named methods run, in the given order, minus any switched off.

Whether a step is switched on is kept where every method's switch lives (`toggles.py`,
also flipped from the dashboard), so there is one switch per method. The chain config
holds the order.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Literal, Optional

from scrapemm.common.paths import APP_NAME
from .config import get_config_var, update_config
from .toggles import is_enabled, set_enabled

logger = logging.getLogger(APP_NAME)

CONFIG_KEY = "retrieval_chain"

LIVE, ARCHIVE = "live", "archive"
Stage = Literal["live", "archive"]


@dataclass(frozen=True)
class MethodInfo:
    key: str  # Stable id in the config, the API and explicit method lists
    name: str  # What the UI shows
    label: str  # What a response names as its method (kept stable for clients)
    stage: Stage
    description: str
    speed: str  # A rough duration, as measured on typical pages
    cost: str  # What using it costs
    local: bool = False  # Fetches from this server's own IP (skipped if the host refuses it)
    available: bool = True  # False for a slot that is not implemented yet


# The steps, in their default order. Measured on the fact-check benchmark: the browser
# succeeds more often than Firecrawl and as often as Decodo, at a fraction of Decodo's time
# and for free; Decodo is paid, hence last among the scrapers; a plain request is the
# cheap last resort for static pages that the services refuse (gov.ru). Among the
# archives, Perma.cc goes first: its lookup answers within a second, and its archives are
# typically those fact-checkers made on purpose; the Wayback Machine's lookup can take
# twenty seconds when archive.org is busy.
METHODS: tuple[MethodInfo, ...] = (
    MethodInfo(
        "integrations", "Platform integrations", "integrations", LIVE,
        "The site's own integration where it has one: X, TikTok, Instagram, Facebook, "
        "Telegram, YouTube, Reddit, Bluesky, Threads, and the archive services. Uses their "
        "APIs or specialised scrapers, and gets posts with their media and metadata.",
        speed="1–15 s", cost="Free; some need API credentials (see Secrets)"),
    MethodInfo(
        "browser", "Browser", "browser", LIVE,
        "scrapeMM's own Chromium on this server. Renders JavaScript and passes Cloudflare "
        "checks by itself. Also steps in for Cloudflare-challenged pages that another "
        "method was routed to.",
        speed="5–30 s", cost="Free, server CPU and memory", local=True),
    MethodInfo(
        "firecrawl", "Firecrawl", "firecrawl", LIVE,
        "The Firecrawl scraping service (self-hosted or cloud). Parses PDFs, which the "
        "browser cannot.",
        speed="5–30 s", cost="Free when self-hosted; needs a reachable instance",
        local=True),
    MethodInfo(
        "decodo", "Decodo", "decodo", LIVE,
        "Decodo's paid scraping API. Fetches through proxies elsewhere, so it reaches sites "
        "that block this server.",
        speed="10–60 s", cost="Paid: one request per try (needs a token)"),
    MethodInfo(
        "plain_http", "Plain request", "Plain HTTP", LIVE,
        "A plain GET of the page's static HTML, without JavaScript. The last resort for "
        "simple pages that scraping services refuse. Open web only.",
        speed="< 3 s", cost="Free", local=True),
    MethodInfo(
        "perma_cc", "Perma.cc", "Perma.cc archive", ARCHIVE,
        "The newest existing Perma.cc archive of the URL — often the one a fact-checker "
        "made of the page. Finding out costs well under a second, so a URL without one "
        "moves on to the Wayback Machine at once.",
        speed="< 1 s lookup, 10–35 s with an archive", cost="Free, no account"),
    MethodInfo(
        "wayback", "Wayback Machine", "Wayback Machine", ARCHIVE,
        "The newest capture in the Internet Archive's Wayback Machine, replayed in the "
        "browser. Gets pages that are gone, down, blocked or gated — as they were when "
        "captured, which may be long ago.",
        speed="10–60 s", cost="Free; archive.org rate-limits heavy use"),
)
METHOD_INFO = {m.key: m for m in METHODS}
DEFAULT_ORDER = [m.key for m in METHODS]

# Former names (and response labels) of the steps, so that old clients' method lists and
# the job history keep resolving
ALIASES = {"plain http": "plain_http", "internet archive (fallback)": "wayback",
           "wayback machine": "wayback", "perma.cc archive": "perma_cc",
           "headed browser": "browser"}

# Domains whose live stage is fixed, see the module docstring. "integrations" expands to
# the domain's integration(s).
DOMAIN_ROUTES: dict[str, list[str]] = {
    # Social media platforms:
    "instagram.com": ["integrations"],
    "facebook.com": ["integrations"],
    "fb.watch": ["integrations"],
    "x.com": ["integrations"],
    "twitter.com": ["integrations"],
    "t.co": ["integrations"],
    "t.me": ["integrations"],
    "tiktok.com": ["integrations"],
    "telegram.me": ["integrations"],
    "bsky.app": ["integrations"],
    "truthsocial.com": ["firecrawl", "plain_http"],
    "reddit.com": ["integrations", "firecrawl", "decodo"],
    "youtube.com": ["integrations"],
    "youtu.be": ["integrations"],
    # Archiving services:
    "archive.today": ["integrations"],
    "archive.is": ["integrations"],
    "archive.ph": ["integrations"],
    "archive.vn": ["integrations"],
    "archive.li": ["integrations"],
    "archive.fo": ["integrations"],
    "archive.md": ["integrations"],
    "perma.cc": ["integrations"],
    "archive.org": ["integrations"],
    "awesomescreenshot.com": ["integrations"],
    # Miscellaneous:
    "washingtonpost.com": ["decodo", "plain_http"],
    "verafiles.org": ["decodo", "firecrawl", "plain_http"],
}

EXCEPTIONS_KEY = "retrieval_exceptions"
LIVE_KEYS = [m.key for m in METHODS if m.stage == LIVE]

# A domain, or "*." and a domain (its subdomains only)
PATTERN = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")


@dataclass
class DomainRule:
    pattern: str
    methods: list[str]

    def to_dict(self) -> dict:
        return {"pattern": self.pattern, "methods": list(self.methods)}


def default_exceptions() -> list[DomainRule]:
    return [DomainRule(domain, list(methods)) for domain, methods in DOMAIN_ROUTES.items()]


def configured_exceptions() -> list[DomainRule]:
    """The saved exceptions, or the defaults if none were ever saved."""
    saved = get_config_var(EXCEPTIONS_KEY)
    if not isinstance(saved, list):
        return default_exceptions()
    try:
        return validate_exceptions(saved)
    except ValueError:
        logger.warning("The saved retrieval exceptions are invalid; using the defaults.",
                       exc_info=True)
        return default_exceptions()


def validate_exceptions(entries: list) -> list[DomainRule]:
    """Checks and normalises `[{"pattern": ..., "methods": [...]}, ...]`. Raises
    ValueError, naming the entry, on anything invalid."""
    result, seen = [], set()
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"Exception {i} is not an object.")
        pattern = str(entry.get("pattern") or "").strip().lower()
        pattern = re.sub(r"^https?://", "", pattern).strip("/")
        if pattern.startswith("www."):
            pattern = pattern[4:]
        if not PATTERN.match(pattern):
            raise ValueError(f"Exception {i}: '{pattern or entry.get('pattern')}' is not a "
                             f"domain (like example.com) or *.domain.")
        if pattern in seen:
            raise ValueError(f"Exception {i}: '{pattern}' appears twice.")
        seen.add(pattern)
        methods = []
        for m in entry.get("methods") or []:
            key = canonical(m)
            if key not in LIVE_KEYS:
                raise ValueError(f"Exception {i} ({pattern}): '{m}' is not a live method. "
                                 f"Allowed: {', '.join(LIVE_KEYS)}.")
            if key in methods:
                raise ValueError(f"Exception {i} ({pattern}): '{key}' appears twice.")
            methods.append(key)
        if not methods:
            raise ValueError(f"Exception {i} ({pattern}) names no method.")
        result.append(DomainRule(pattern, methods))
    return result


def save_exceptions(entries: list) -> list[DomainRule]:
    exceptions = validate_exceptions(entries)
    update_config(**{EXCEPTIONS_KEY: [e.to_dict() for e in exceptions]})
    logger.info(f"Retrieval exceptions set: {len(exceptions)} domain(s).")
    return exceptions


def reset_exceptions() -> list[DomainRule]:
    return save_exceptions([e.to_dict() for e in default_exceptions()])


def match_exception(host: str, exceptions: list[DomainRule]) -> Optional[DomainRule]:
    """The most specific exception whose pattern covers `host`."""
    host = (host or "").lower().rstrip(".")
    if host.startswith("www."):  # www.example.com is example.com itself, not a subdomain
        host = host[4:]
    best = None
    for e in exceptions:
        if e.pattern.startswith("*."):
            base = e.pattern[2:]
            hit = host.endswith("." + base)
        else:
            base = e.pattern
            hit = host == base or host.endswith("." + base)
        if hit and (best is None or len(base) > len(best[0])):
            best = (base, e)
    return best[1] if best else None


# Integrations whose URLs are archives themselves, which the archive stage skips
ARCHIVE_INTEGRATIONS = {"internet archive", "perma.cc", "archive.today", "ghostarchive"}

# Platforms whose content the archive services do not preserve: their URLs skip the
# archive stage. Not TikTok: Wayback captures of TikTok videos do hold the video.
ARCHIVES_USELESS_FOR = {"YouTube"}

# Platform URLs whose content is a video: an archived copy of one counts only if it holds
# the video (see `engine._unusable_platform_copy()`). A Wayback capture of a YouTube watch
# page has the thumbnails and the comments, never the video.
VIDEO_URL = re.compile(r"""(?ix)
      youtube\.com/(watch|shorts/|live/|embed/|v/) | youtu\.be/
    | tiktok\.com/(@[^/]+/video/|v/|t/) | (vm|vt)\.tiktok\.com/
    | facebook\.com/(reel/|watch|[^/?]+/videos/|video\.php) | fb\.watch/
    | instagram\.com/(reel|reels|tv)/
    | (x|twitter)\.com/[^/]+/status/\d+/video/
    | kwai\.com/.*/video/
""")


def is_video_url(url: str) -> bool:
    return bool(VIDEO_URL.search(url))


def platform_of(url: str) -> Optional[str]:
    """The platform integration responsible for the URL's domain (X, TikTok, YouTube, ...),
    or None for the open web and for archive services' own URLs."""
    from .integrations import get_integrations_for_url
    names = [n for n in get_integrations_for_url(url) if n.lower() not in ARCHIVE_INTEGRATIONS]
    return names[0] if names else None


def canonical(name: str) -> str:
    """The chain key for a step named any way (case, old names); anything else (an
    integration's name) is returned unchanged."""
    lowered = str(name).strip().lower()
    lowered = ALIASES.get(lowered, lowered)
    return lowered if lowered in METHOD_INFO else str(name)


def label(key: str) -> str:
    """The name a response gives the method: a step's label, or the integration's name."""
    info = METHOD_INFO.get(key)
    return info.label if info else key


def key_of(label_or_name: str) -> str:
    """Inverse of `label()`: the chain key of a response's method name, if it is a step."""
    return canonical(label_or_name)


# --- The configured chain -----------------------------------------------------------

@dataclass
class Step:
    method: str
    enabled: bool

    def to_dict(self) -> dict:
        return {"method": self.method, "enabled": self.enabled}


def configured_order() -> list[str]:
    """The saved order, validated: unknown entries dropped, missing ones added at their
    default position (so a method introduced by an update shows up)."""
    saved = get_config_var(CONFIG_KEY)
    order = [canonical(m) for m in saved] if isinstance(saved, list) else []
    return normalize_order(order)


def normalize_order(order: list[str]) -> list[str]:
    seen, cleaned = set(), []
    for key in order:
        if key in METHOD_INFO and key not in seen:
            seen.add(key)
            cleaned.append(key)
    for key in DEFAULT_ORDER:  # Newly introduced methods, at their default position
        if key not in seen:
            default_index = DEFAULT_ORDER.index(key)
            # After the last configured method that precedes it by default
            predecessors = [cleaned.index(k) for k in DEFAULT_ORDER[:default_index] if k in cleaned]
            cleaned.insert(max(predecessors) + 1 if predecessors else 0, key)
            seen.add(key)
    return cleaned


def configured_chain() -> list[Step]:
    return [Step(key, is_enabled(key)) for key in configured_order()]


def default_chain() -> list[Step]:
    return [Step(key, True) for key in DEFAULT_ORDER]


def save_chain(steps: list[dict]) -> list[Step]:
    """Validates and stores a chain: `[{"method": key, "enabled": bool}, ...]`.
    Raises ValueError on unknown or duplicate methods."""
    keys = []
    for step in steps:
        if not isinstance(step, dict) or "method" not in step:
            raise ValueError(f"Each step needs a 'method': {step!r}")
        key = canonical(step["method"])
        if key not in METHOD_INFO:
            raise ValueError(f"Unknown method '{step['method']}'. Known: {', '.join(DEFAULT_ORDER)}.")
        if key in keys:
            raise ValueError(f"'{key}' appears twice.")
        keys.append(key)
    order = normalize_order(keys)
    update_config(**{CONFIG_KEY: order})
    wanted = {canonical(s["method"]): bool(s.get("enabled", True)) for s in steps}
    for key in order:
        if key in wanted and is_enabled(key) != wanted[key]:
            set_enabled(key, wanted[key])
    logger.info(f"Retrieval chain set to: {', '.join(k if is_enabled(k) else f'({k})' for k in order)}.")
    return configured_chain()


def reset_chain() -> list[Step]:
    return save_chain([s.to_dict() for s in default_chain()])


# --- Resolution ---------------------------------------------------------------------

@dataclass
class Plan:
    """What the engine does for one URL."""
    domain: str
    integrations: list[str] = field(default_factory=list)  # The domain's own integrations
    exception: Optional[DomainRule] = None  # The exception that replaced the live methods
    order: list[str] = field(default_factory=list)  # What runs, in order: steps, integrations
    live: list[str] = field(default_factory=list)  # The live ones of `order`, in order
    archive: list[str] = field(default_factory=list)  # The archive ones, in order
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (method, why)

    @property
    def methods(self) -> list[str]:
        return list(self.order)

    @property
    def route(self) -> Optional[list[str]]:
        return self.exception.methods if self.exception else None

    def to_dict(self) -> dict:
        def describe(key: str) -> dict:
            info = METHOD_INFO.get(key)
            return {"method": key, "name": info.name if info else key,
                    "stage": info.stage if info else LIVE,
                    "integration": info is None}
        return {"domain": self.domain, "integrations": self.integrations,
                "exception": self.exception.to_dict() if self.exception else None,
                "route": self.route,  # Former name of the exception's methods
                "steps": [describe(k) for k in self.order],
                "live": [describe(k) for k in self.live],
                "archive": [describe(k) for k in self.archive],
                "skipped": [{**describe(k), "reason": why} for k, why in self.skipped]}


def resolve(url: str, methods: Literal["auto"] | list[str] = "auto",
            chain: Optional[list[dict]] = None,
            exceptions: Optional[list] = None) -> Plan:
    """The methods to try for `url`, in order. `methods` is "auto" (the configured chain
    and exceptions) or a client's explicit list. `chain` and `exceptions` replace the
    configured ones (the UI's preview of unsaved changes)."""
    from urllib.parse import urlsplit
    from .integrations import get_integrations_for_url
    from .util import get_domain

    domain = get_domain(url) or ""
    plan = Plan(domain=domain, integrations=get_integrations_for_url(url))
    archive_url = any(name.lower() in ARCHIVE_INTEGRATIONS for name in plan.integrations)
    unarchived = next((n for n in plan.integrations if n in ARCHIVES_USELESS_FOR), None)

    if chain is not None:
        order = normalize_order([canonical(s["method"]) for s in chain])
        switches = {canonical(s["method"]): bool(s.get("enabled", True)) for s in chain}
        step_on = lambda key: switches.get(key, is_enabled(key))  # noqa: E731
    else:
        order = configured_order()
        step_on = is_enabled

    if methods == "auto":
        rules = (validate_exceptions(exceptions) if exceptions is not None
                 else configured_exceptions())
        try:
            host = urlsplit(url).hostname or domain
        except ValueError:
            host = domain
        plan.exception = match_exception(host, rules)
        if plan.exception:
            # Its methods take the place of the chain's live ones, at the first one's
            replaced, placed = [], False
            for key in order:
                if METHOD_INFO[key].stage == ARCHIVE:
                    replaced.append(key)
                elif not placed:
                    replaced += plan.exception.methods
                    placed = True
            for key in order:
                if METHOD_INFO[key].stage == LIVE and key not in plan.exception.methods:
                    plan.skipped.append((key, f"the exception for {plan.exception.pattern} "
                                              f"uses {', '.join(_names(plan.exception.methods))}"))
            order = replaced
    else:
        order = [canonical(m) for m in methods]

    for key in order:
        info = METHOD_INFO.get(key)
        if info is None:  # An integration named explicitly
            _add_integration(plan, key)
        elif not step_on(key):
            plan.skipped.append((key, "switched off"))
        elif info.stage == ARCHIVE:
            if not info.available:
                plan.skipped.append((key, "not available yet"))
            elif archive_url:
                plan.skipped.append((key, "the URL is an archive itself"))
            elif unarchived:
                plan.skipped.append((key, f"archives do not preserve {unarchived}'s videos"))
            elif key == "wayback" and not is_enabled("Internet Archive"):
                plan.skipped.append((key, "needs the Internet Archive integration, which is off"))
            elif key == "perma_cc" and not is_enabled("Perma.cc"):
                plan.skipped.append((key, "needs the Perma.cc integration, which is off"))
            else:
                plan.order.append(key)
        elif key == "integrations":
            if not plan.integrations:
                plan.skipped.append((key, f"no integration for {domain or 'this URL'}"))
            for name in plan.integrations:
                _add_integration(plan, name)
        elif key == "plain_http" and plan.integrations and methods == "auto":
            plan.skipped.append((key, "not used for platform domains"))
        else:
            plan.order.append(key)
    plan.live = [k for k in plan.order if stage_of(k) == LIVE]
    plan.archive = [k for k in plan.order if stage_of(k) == ARCHIVE]
    return plan


def stage_of(key: str) -> Stage:
    """A step's stage; an integration is a live method."""
    info = METHOD_INFO.get(key)
    return info.stage if info else LIVE


def _add_integration(plan: Plan, name: str) -> None:
    if is_enabled(name):
        if name not in plan.order:
            plan.order.append(name)
    else:
        plan.skipped.append((name, "switched off"))


def _names(keys: list[str]) -> list[str]:
    return [METHOD_INFO[k].name if k in METHOD_INFO else k for k in keys]
