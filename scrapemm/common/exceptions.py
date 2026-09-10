class UnsupportedDomainError(Exception):
    """The domain is not supported by the scraping method or scrapeMM overall."""


class RateLimitError(Exception):
    """The service's rate limit has been reached. No further requests
    are allowed at the moment, but can be tried later."""


class QuotaExceededError(Exception):
    """The service's quota has been exceeded. No further requests
    are possible anymore unless the user recharges the service's quota."""


class RetrievalFailed(Exception):
    """scrapeMM failed to retrieve the content from the given URL. The
    root cause is not a bug in scrapeMM's code but the lacking abilities
    of the current scraping implementation."""


class AccessBlockedError(Exception):
    """The target could be reached, but the content is actively hidden by the
    platform's content moderation, prohibiting (automated) access or
    access for specific user groups or regions. Typical case of a 403 error."""


class CaptchaEncounteredError(AccessBlockedError):
    """The target could be reached, but the content is protected by a CAPTCHA
    that needs to be solved before it can be accessed."""


class TargetUnavailableError(Exception):
    """The target cannot be reached at all, e.g., because the DNS
    did not resolve or the target's server is down. Typical case of
    5xx errors or 404."""


class DiskFull(Exception):
    """No space left on disk. The application should be aborted immediately
    before continuing."""
