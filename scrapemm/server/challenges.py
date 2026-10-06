"""CAPTCHA challenges waiting for a human, for every site.

When all methods for a URL fail and at least one of them ran into a CAPTCHA, the domain
gets a *challenge*: the URL is queued with it, and so is every further URL of that
domain for as long as the challenge is open -- scraping it again would only run into
the same wall. Somebody then decides in the web UI:

* **Solve** -- the server's headed browser opens the challenge page on the virtual
  display, the human passes the check in the CAPTCHA panel, and the queued URLs are
  retrieved right away with that browser. Its persistent profile keeps the clearance
  the check handed out, which is what lets the retrieval through. Results go into the
  result cache, so asking for a URL again returns it.
* **Discard** -- the queued URLs are dropped and the domain is blacklisted for a while,
  which is what used to happen to every CAPTCHA-protected domain right away.

Archive.today keeps its own machinery (a session cookie reusable over plain HTTP, and a
permanent page cache), but it is listed here as a challenge like any other, so the UI
has one place and one set of decisions for all of them.

Domains an integration serves are recorded but never *held*: holding x.com would stop
the X integration, which does not care about a CAPTCHA some other method ran into.
"""

import asyncio
import json
import logging
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field, asdict
from typing import Optional

from scrapemm.common.paths import APP_NAME
from .paths import CONFIG_DIR

logger = logging.getLogger(APP_NAME)

CHALLENGES_PATH = CONFIG_DIR / "challenges.json"

# Detections a human reported as wrong ("No CAPTCHA here"), with page snapshots
FALSE_POSITIVES_DIR = CONFIG_DIR / "captcha_false_positives"
MAX_FALSE_POSITIVE_SNAPSHOTS = 50

# Per domain, the (URL, page) the detection flagged last while draining the queue
_last_flagged: dict[str, tuple[str, str]] = {}

# Queued URLs per domain. A crawl into a gated site must not grow this without bound.
MAX_PENDING = 1000

# Archive.today's challenge, as listed alongside the generic ones
ARCHIVE_TODAY = "archive.ph"

# Archives that replay what they captured. A CAPTCHA seen in their content is part of
# the capture -- a GeeTest in a Perma.cc replay of a TikTok page, say -- frozen, and no
# human can solve it; their own live checks (Cloudflare, Anubis) the browser passes by
# itself. Queueing their URLs for a human only held them until somebody gave up.
REPLAYING_ARCHIVES = frozenset({"perma.cc", "archive.org", "ghostarchive.org"})

# Sites that put every visitor behind a CAPTCHA, by the CAPTCHA they show. Scraping them
# only costs each method its timeout before failing the same way, so their URLs go
# straight into the queue for a human, as if a challenge were always open.
ALWAYS_GATED = {"researchgate.net": "DataDome"}


@dataclass
class Pending:
    """A queued URL, with what is needed to answer the request it came from."""
    url: str
    output_format: str = "multimodal"
    methods: list[str] = field(default_factory=list)  # As resolved, for the cache key
    max_video_size: Optional[int] = None
    queued_at: float = field(default_factory=time.time)


@dataclass
class Challenge:
    domain: str
    captcha: str  # What was detected, e.g. "Cloudflare challenge"
    solve_url: str  # Where the human solves it: the first URL that ran into it
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    pending: list[Pending] = field(default_factory=list)


class ChallengeStore:
    """The open challenges, persisted so a restart does not lose the queue."""

    def __init__(self, path=CHALLENGES_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._challenges: dict[str, Challenge] = self._load()

    def record(self, domain: str, url: str, captcha: str, output_format: str,
               methods: list[str], max_video_size: Optional[int]) -> None:
        """Opens a challenge for the domain, or queues the URL with the open one."""
        if domain in REPLAYING_ARCHIVES:
            logger.info(f"Not queueing {url} for a human: the {captcha} is part of the "
                        f"archived copy, which nobody can solve.")
            return
        with self._lock:
            challenge = self._challenges.get(domain)
            if challenge is None:
                challenge = Challenge(domain=domain, captcha=captcha, solve_url=url)
                self._challenges[domain] = challenge
                logger.warning(f"🤖 {domain} is behind a {captcha}. Solve or discard it "
                               f"on the CAPTCHA page of the web UI.")
            challenge.last_seen = time.time()
            if not any(p.url == url and p.output_format == output_format
                       for p in challenge.pending):
                if len(challenge.pending) < MAX_PENDING:
                    challenge.pending.append(Pending(url, output_format, methods, max_video_size))
                else:
                    logger.warning(f"Not queueing {url}: {MAX_PENDING} URLs are already "
                                   f"waiting on the {domain} CAPTCHA.")
            self._save()

    def holds(self, domain: str) -> bool:
        """Whether new URLs of this domain should wait instead of being scraped."""
        from .integrations import DOMAIN_TO_INTEGRATION
        if domain in ALWAYS_GATED:
            return True
        return domain in self._challenges and domain not in DOMAIN_TO_INTEGRATION

    def get(self, domain: str) -> Optional[Challenge]:
        return self._challenges.get(domain)

    def captcha_for(self, domain: str) -> str:
        """The CAPTCHA a held domain's URLs wait on, for the challenge they are queued with."""
        if challenge := self._challenges.get(domain):
            return challenge.captcha
        return ALWAYS_GATED.get(domain, "CAPTCHA")

    def all(self) -> list[Challenge]:
        return sorted(self._challenges.values(), key=lambda c: c.first_seen)

    def keep_pending(self, domain: str, pending: list[Pending]) -> None:
        """Replaces the queue; an emptied one closes the challenge."""
        with self._lock:
            if domain not in self._challenges:
                return
            if pending:
                self._challenges[domain].pending = pending
            else:
                del self._challenges[domain]
                logger.info(f"✅ The {domain} CAPTCHA challenge is resolved.")
            self._save()

    def remove(self, domain: str) -> Optional[Challenge]:
        with self._lock:
            challenge = self._challenges.pop(domain, None)
            self._save()
            return challenge

    def _load(self) -> dict[str, Challenge]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning(f"Ignoring the unreadable challenge list at {self.path}.")
            return {}
        challenges = {}
        for entry in raw:
            if entry.get("domain") in REPLAYING_ARCHIVES:
                logger.info(f"Dropping the {entry['domain']} CAPTCHA challenge: its CAPTCHA "
                            f"is part of the archived copies, which nobody can solve.")
                continue
            try:
                pending = [Pending(**p) for p in entry.pop("pending", [])]
                challenges[entry["domain"]] = Challenge(**entry, pending=pending)
            except (TypeError, KeyError):
                logger.debug(f"Skipping a malformed challenge entry: {entry!r}")
        return challenges

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            partial = self.path.with_suffix(".json.part")
            partial.write_text(json.dumps([asdict(c) for c in self._challenges.values()],
                                          indent=1), encoding="utf-8")
            partial.replace(self.path)
        except OSError:
            logger.warning(f"Could not save the challenge list to {self.path}.", exc_info=True)


store = ChallengeStore()


# --- Listing ------------------------------------------------------------------------

def list_challenges() -> list[dict]:
    """Every challenge waiting for a decision, Archive.today's included, as the UI
    shows them."""
    listed = []
    if archive := _archive_today_challenge():
        listed.append(archive)
    for challenge in store.all():
        listed.append({
            "domain": challenge.domain,
            "captcha": challenge.captcha,
            "solve_url": challenge.solve_url,
            "first_seen": challenge.first_seen,
            "last_seen": challenge.last_seen,
            "urls": [p.url for p in challenge.pending],
            "waiting": len(challenge.pending),
            "held": store.holds(challenge.domain),
            "kind": "generic",
        })
    return listed


def waiting_count() -> int:
    return sum(c["waiting"] for c in list_challenges())


def _archive_today_challenge() -> Optional[dict]:
    try:
        from .integrations.archive_today import get_archive_today_buffer
        buffered = get_archive_today_buffer()
    except Exception:
        logger.debug("Could not read the Archive.today buffer.", exc_info=True)
        return None
    if not buffered:
        return None
    return {
        "domain": ARCHIVE_TODAY,
        "captcha": "Archive.today security check",
        "solve_url": f"https://{ARCHIVE_TODAY}/",
        "first_seen": None,
        "last_seen": None,
        "urls": buffered,
        "waiting": len(buffered),
        "held": False,
        "kind": "archive_today",
    }


# --- Deciding -----------------------------------------------------------------------

def discard(domain: str) -> int:
    """Drops the queued URLs. A domain nothing else serves is blacklisted for a while,
    so it is not scraped straight back into the same CAPTCHA."""
    if domain == ARCHIVE_TODAY:
        from .integrations.archive_today import (clear_archive_today_buffer,
                                                 get_archive_today_buffer)
        dropped = len(get_archive_today_buffer())
        clear_archive_today_buffer()
        return dropped

    challenge = store.remove(domain)
    if challenge is None:
        return 0
    from .blacklist import blacklist
    from .integrations import DOMAIN_TO_INTEGRATION
    if domain not in DOMAIN_TO_INTEGRATION:
        blacklist.add(domain, f"The {challenge.captcha} was discarded in the web UI "
                              f"(first hit at {challenge.solve_url}).")
    logger.info(f"Discarded the {domain} CAPTCHA challenge with "
                f"{len(challenge.pending)} queued URL(s).")
    return len(challenge.pending)


async def await_solution(domain: str, timeout: float,
                         no_captcha: Optional[asyncio.Event] = None) -> tuple[bool, bool]:
    """Opens the challenge page for the human and waits until the check is passed. Only
    the human's part: retrieving the queue afterwards is `drain()`, which the caller
    runs in the background so the panel can close at once. Returns (passed, whether
    the "no CAPTCHA" report was applied).

    Setting `no_captcha` says the human sees no check at all, only the normal page: the
    detection was presumably wrong. The case is recorded for debugging, and the queue
    should then be drained with `trust_content`, since asking the detection again would
    only repeat the mistake."""
    from .integrations.headed_browser import human_tab
    if domain == ARCHIVE_TODAY:
        from .integrations import NAME_TO_INTEGRATION
        from .integrations.archive_today import CANONICAL_DOMAIN, VERIFICATION_SNAPSHOT
        with human_tab():
            passed = await NAME_TO_INTEGRATION["archive.today"]._solve_in_browser(
                f"https://{CANONICAL_DOMAIN}/{VERIFICATION_SNAPSHOT}", timeout)
        return passed, False

    challenge = store.get(domain)
    if challenge is None:
        raise KeyError(f"There is no open CAPTCHA challenge for {domain}.")
    no_captcha = no_captcha or asyncio.Event()
    with human_tab():
        passed = await _await_human(challenge, timeout, no_captcha)
    return passed, passed and no_captcha.is_set()


async def confirm_no_captcha(domain: str) -> tuple[int, int]:
    """After a session: the human saw no check, yet the queue stayed gated. Records the
    content the detection flagged last and retrieves the queue without the detection.
    Returns (retrieved, still waiting)."""
    challenge = store.get(domain)
    if challenge is None:
        raise KeyError(f"There is no open CAPTCHA challenge for {domain}.")
    url, html = _last_flagged.pop(domain, (challenge.solve_url, ""))
    _report_false_positive(challenge, url, html)
    return await drain(domain, trust_content=True)


async def drain(domain: str, trust_content: bool = False) -> tuple[int, int]:
    """Retrieves the queued URLs with the browser and caches them. Stops at the first
    one that is gated again: the clearance has run out, and the rest would be too.
    With `trust_content`, the CAPTCHA detection is not consulted (see `solve()`).
    Returns (retrieved, still waiting)."""
    if domain == ARCHIVE_TODAY:
        from .integrations.archive_today import (get_archive_today_buffer,
                                                 retrieve_buffered_archive_today)
        retrieved = await retrieve_buffered_archive_today()
        return retrieved, len(get_archive_today_buffer())

    challenge = store.get(domain)
    if challenge is None:
        return 0, 0

    from scrapemm.common import ScrapingResponse
    from .cache import cache, cache_key
    from .captcha_detect import detect_captcha
    from .engine import postprocess_media

    browser = _browser_for(domain)
    remaining = list(challenge.pending)
    retrieved = 0
    while remaining:
        entry = remaining[0]
        started = time.time()
        try:
            content = await browser._get(entry.url, output_format=entry.output_format)
        except Exception as e:
            if "captcha" in type(e).__name__.lower():
                break
            # Not the CAPTCHA's fault; waiting longer would not help this URL
            logger.info(f"Could not retrieve the queued {entry.url}: {type(e).__name__}: {e}")
            remaining.pop(0)
            continue
        if not trust_content and detect_captcha(content):
            logger.info(f"{domain} is gated again; {len(remaining)} URL(s) keep waiting.")
            # Kept as evidence, should the human report that there is no check
            _last_flagged[domain] = (entry.url, content.html or str(content.multimodal or ""))
            break
        remaining.pop(0)
        if content.get(entry.output_format) is None:
            logger.info(f"The queued {entry.url} yielded no {entry.output_format} content.")
            continue
        if content.multimodal is not None:
            await postprocess_media(content.multimodal)
        response = ScrapingResponse(url=entry.url, content=content, method=browser.name,
                                    output_format=entry.output_format,
                                    retrieval_time=time.time() - started)
        cache.put(cache_key(entry.url, entry.output_format, entry.methods,
                            entry.max_video_size), response)
        retrieved += 1

    store.keep_pending(domain, remaining)
    return retrieved, len(remaining)


def _browser_for(domain: str):
    """The browser integration that should retrieve the queue: the domain's own if it is
    browser-based (e.g. Perma.cc), the Browser method otherwise. Either way it is the one
    shared browser, whose profile holds the clearance."""
    from .integrations import DOMAIN_TO_INTEGRATION, browser
    from .integrations.headed_browser import HeadedBrowser
    integration = DOMAIN_TO_INTEGRATION.get(domain)
    if isinstance(integration, HeadedBrowser):
        return integration
    return browser


# A CAPTCHA widget on screen: a large visible frame, or overlay element, that names a
# CAPTCHA.
# Checked besides the page's markup, since some sites lay their check over a fully
# loaded page (TikTok): the markup then looks like content, while the human still has a
# check to pass. Small ones are left out, e.g. reCAPTCHA's "protected by" badge.
_VISIBLE_CAPTCHA_JS = r"""() => {
  const shown = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width >= 150 && r.height >= 150 && s.visibility !== 'hidden'
      && s.display !== 'none' && Number(s.opacity) > 0.05; };
  const hosts = /captcha|challenges\.cloudflare|geetest|captcha-delivery|arkoselabs/i;
  for (const f of document.querySelectorAll('iframe'))
    if (hosts.test(f.src) && shown(f)) return 'frame ' + f.src.slice(0, 100);
  // Elements only as overlays (in a fixed layer), not e.g. a form of class "captcha-form"
  const overlaid = e => { for (; e; e = e.parentElement)
    if (getComputedStyle(e).position === 'fixed') return true; return false; };
  for (const e of document.querySelectorAll('[id*=captcha i], [class*=captcha i]'))
    if (shown(e) && overlaid(e)) return e.tagName.toLowerCase() + '#' + e.id + '.' + String(e.className).slice(0, 60);
  return null;
}"""

# Counts the pointer input that reaches the page and its frames, so a check that "does
# not react" to the human can be told apart from input that never arrived
_COUNT_INPUT_JS = """(() => { if (window.__scrapemmInput) return; const n = window.__scrapemmInput = {};
  for (const t of ['pointerdown', 'pointermove', 'pointerup', 'pointercancel', 'mousedown',
                   'mousemove', 'mouseup', 'touchstart', 'dragstart'])
    addEventListener(t, e => { const k = t + (e.buttons ? '+held' : ''); n[k] = (n[k] || 0) + 1; }, true);
})()"""


async def _await_human(challenge: Challenge, timeout: float,
                       no_captcha: asyncio.Event) -> bool:
    """Shows the challenge page in the server's browser and waits until it is passed,
    or until the human reports that there is no check to pass. Call it within
    `human_tab()`, which keeps the page in front of the tabs retrievals open meanwhile.

    Passed means: neither the markup nor the screen shows a CAPTCHA, twice in a row --
    right after loading, many pages show no check *yet*."""
    from .integrations.headed_browser import keep_in_front, release_page_soon
    from scrapemm.common import ScrapedContent
    from .captcha_detect import detect_captcha

    url = challenge.solve_url
    browser = _browser_for("")  # The generic one; the page is all that matters here
    deadline = time.time() + timeout
    page, _ = await browser._new_page()
    clean_polls = 0
    try:
        with suppress(Exception):
            await page.add_init_script(_COUNT_INPUT_JS)
        await page.goto(url, timeout=60_000, wait_until="domcontentloaded")
        with suppress(Exception):
            await page.wait_for_load_state("load", timeout=10_000)
        while True:
            try:
                html = await page.content()
                shown = await page.evaluate(_VISIBLE_CAPTCHA_JS)
            except Exception:
                html, shown = "", None  # Mid-navigation, which passing a check often causes
            if no_captcha.is_set():
                _report_false_positive(challenge, page.url, html)
                return True
            clean = bool(html) and not shown and not detect_captcha(ScrapedContent(html=html))
            clean_polls = clean_polls + 1 if clean else 0
            if clean_polls >= 2:
                logger.info(f"The CAPTCHA at {url} was passed.")
                return True
            if time.time() >= deadline:
                # Evidence, should the human report afterwards that there was no check
                if html:
                    _last_flagged[challenge.domain] = (page.url, html)
                logger.info(f"The CAPTCHA at {url} was not passed in time"
                            f"{f' (still shown: {shown})' if shown else ''}.")
                return False
            # Should anything have come to the front after all (a popup, say)
            await keep_in_front(page)
            # Woken right away by the "no CAPTCHA" report
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(no_captcha.wait(), timeout=1)
    finally:
        await _log_input(page)
        # Closed in the background: under load, closing a tab can take seconds, and
        # the panel should not stay open for that
        release_page_soon(page)


async def _log_input(page) -> None:
    """Logs the pointer input the CAPTCHA page received, per frame (see
    `_COUNT_INPUT_JS`)."""
    counts = []
    for frame in page.frames:
        with suppress(Exception):
            n = await asyncio.wait_for(frame.evaluate("window.__scrapemmInput || null"), 2)
            if n:
                counts.append(f"{frame.url[:80]}: {n}")
    if counts:
        logger.info("Pointer input on the CAPTCHA page: " + "; ".join(counts))


def _report_false_positive(challenge: Challenge, page_url: str, html: str) -> None:
    """Records a detection the human saw no CAPTCHA for: a line in the log file, and
    the page as it was shown, so the detection can be fixed later."""
    from scrapemm.common import ScrapedContent
    from .captcha_detect import explain

    detection = explain(ScrapedContent(html=html)) if html else {}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    snapshot = FALSE_POSITIVES_DIR / f"{stamp}_{challenge.domain}.html"
    try:
        FALSE_POSITIVES_DIR.mkdir(parents=True, exist_ok=True)
        if html:
            snapshot.write_text(html, encoding="utf-8")
        entry = {"time": time.time(), "domain": challenge.domain,
                 "detected": challenge.captcha, "solve_url": challenge.solve_url,
                 "page_url": page_url, "snapshot": snapshot.name if html else None,
                 "detection_now": detection}
        with open(FALSE_POSITIVES_DIR / "reports.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        # Snapshots can be large; the most recent ones are all debugging needs
        for old in sorted(FALSE_POSITIVES_DIR.glob("*.html"))[:-MAX_FALSE_POSITIVE_SNAPSHOTS]:
            old.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not record the CAPTCHA false positive.", exc_info=True)

    logger.warning(
        f"🕵️ Possible false CAPTCHA detection: no check was shown at {page_url} "
        f"(detected: {challenge.captcha}). Detection on the page now: "
        f"{detection.get('verdict')}, text length {detection.get('text_length')}, "
        f"matches {detection.get('matches')}. Recorded in {FALSE_POSITIVES_DIR}.")
