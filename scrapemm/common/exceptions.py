class RateLimitError(Exception):
    pass


class QuotaExceededError(Exception):
    pass


class IPBannedError(Exception):
    pass


class RetrievalFailed(Exception):
    pass


class ContentBlockedError(Exception):
    """The content was found but is blocked by content moderation, prohibiting
    automated access (manual access might work, though)."""
    pass


class UnsupportedDomainError(Exception):
    """The domain is not supported by the scraper."""
    pass


class TargetUnavailableError(Exception):
    """The target website is not available for scraping."""


class DiskFull(Exception):
    """No space left on disk. The application should be aborted immediately before continuing."""
