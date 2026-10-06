"""How a retrieval came out, in three classes -- the one place that decides it.

    ok           green   The content was retrieved.
    unavailable  yellow  scrapeMM worked, but the target did not let it have the content:
                         it is gone, behind a paywall, a CAPTCHA or a login, the platform
                         throttles, or the URL is one scrapeMM deliberately does not serve.
                         Counts as a success of scrapeMM.
    error        red     Something went wrong on scrapeMM's end: a method failed
                         (RetrievalFailed), crashed (RuntimeError, any unexpected
                         exception), timed out, or a paid service's quota ran out.

The classes by exception (a subclass is judged by its own entry first):

    TargetUnavailableError   unavailable / missing      gone (404/410), host down or unknown
    PaywallError             unavailable / paywall      only the teaser came through
    CaptchaEncounteredError  unavailable / captcha      waits for a human
    AccessBlockedError       unavailable / blocked      login, age gate, region, HTTP 403
    RateLimitError           unavailable / rate_limit   the platform throttles, for now
    UnsupportedDomainError,
    DomainBlacklistedError   unavailable / unsupported  not served, by design or by setting
    QuotaExceededError       error                      our own service account is used up:
                                                        somebody here has to act
    RetrievalFailed, TimeoutError, RuntimeError, anything else   error

A URL that failed on several methods is classified by its *decisive* error: if any method
found the target unavailable, the URL is unavailable, whatever the other methods ran into
-- an authoritative "this page is gone" or "this is behind a CAPTCHA" explains every
other method's failure too. Among several such findings, the most telling wins, in the
order of UNAVAILABLE_PRIORITY. Only a failure without any of them is red.

`ScrapingResponse.success` is untouched by all this; the classes are additive, for the
job history and the web UI. The UI keeps a copy of the tables in
`ui/app/composables/useOutcome.ts` -- change both together.
"""

from typing import Any, Literal, Mapping, Optional

Outcome = Literal["ok", "unavailable", "error"]

OK: Outcome = "ok"
UNAVAILABLE: Outcome = "unavailable"
ERROR: Outcome = "error"

# Error type name -> kind of unavailability
UNAVAILABLE_KINDS: dict[str, str] = {
    "TargetUnavailableError": "missing",
    "PaywallError": "paywall",
    "CaptchaEncounteredError": "captcha",
    "AccessBlockedError": "blocked",
    "RegionBlockedError": "blocked",
    "RateLimitError": "rate_limit",
    "DomainBlacklistedError": "unsupported",
    "UnsupportedDomainError": "unsupported",
}

# Most telling first: which kind decides when methods found different ones
UNAVAILABLE_PRIORITY = ["missing", "paywall", "captcha", "blocked", "rate_limit", "unsupported"]

# The kind reported for red results
ERROR_KIND = "error"


def _error_type(error: Any) -> Optional[str]:
    """The type name of an error as the engine or the job history holds it: an exception,
    or its wire form {type, message}."""
    if error is None:
        return None
    if isinstance(error, Mapping):
        return error.get("type")
    if isinstance(error, BaseException):
        # By the most specific class that has an entry: a subclass of a listed class
        # (e.g. a TimeoutError subclass, or a future AccessBlockedError subclass) is
        # judged like its parent
        for cls in type(error).__mro__:
            if cls.__name__ in UNAVAILABLE_KINDS:
                return cls.__name__
        return type(error).__name__
    return str(error)


def classify(success: bool, errors: Optional[Mapping[str, Any]] = None) -> tuple[Outcome, Optional[str]]:
    """The outcome of one URL and, unless it is ok, its kind: one of UNAVAILABLE_PRIORITY
    for unavailable results, ERROR_KIND for errors. `errors` maps method names to the
    errors they raised (exceptions or their wire form)."""
    if success:
        return OK, None
    kinds = {UNAVAILABLE_KINDS.get(_error_type(e) or "") for e in (errors or {}).values()}
    for kind in UNAVAILABLE_PRIORITY:
        if kind in kinds:
            return UNAVAILABLE, kind
    return ERROR, ERROR_KIND


def decisive_error(errors: Optional[Mapping[str, Any]]) -> Optional[tuple[str, Any]]:
    """The (method, error) that decided the outcome of a failed URL: the most telling
    unavailability, or else the first error. None if there are no errors."""
    items = [(m, e) for m, e in (errors or {}).items() if e is not None]
    if not items:
        return None

    def rank(item):
        kind = UNAVAILABLE_KINDS.get(_error_type(item[1]) or "")
        return UNAVAILABLE_PRIORITY.index(kind) if kind else len(UNAVAILABLE_PRIORITY)

    return min(items, key=rank)
