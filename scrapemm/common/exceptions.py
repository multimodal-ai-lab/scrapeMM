class RateLimitError(Exception):
    pass


class IPBannedError(Exception):
    pass


class ContentNotFoundError(Exception):
    pass


class ContentBlockedError(Exception):
    """The content was found but is blocked by content moderation, prohibiting
    automated access (manual access might work, though)."""
    pass


class UnsupportedDomainError(Exception):
    """The domain is not supported by the scraper."""
    pass
