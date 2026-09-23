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


class ServerError(Exception):
    """The scrapeMM server itself failed, or the client could not reach it."""


# Exceptions travel over the API as their name plus their message, so that client code
# keeps catching the very same classes it caught when scrapeMM ran in-process. Only the
# classes listed here survive the trip; anything else arrives as a plain RetrievalFailed,
# which is what an unknown failure of a retrieval method amounts to for the caller.
WIRE_EXCEPTIONS: dict[str, type[Exception]] = {
    cls.__name__: cls for cls in (
        UnsupportedDomainError,
        RateLimitError,
        QuotaExceededError,
        RetrievalFailed,
        AccessBlockedError,
        CaptchaEncounteredError,
        TargetUnavailableError,
        DiskFull,
        ServerError,
        NotImplementedError,
        TimeoutError,
        ValueError,
    )
}


def exception_to_wire(e: Exception) -> dict[str, str]:
    """Renders an exception for transport. Unknown classes keep their real name in
    `type` so that it still shows up in logs and in the UI, even though the client
    will reconstruct them as RetrievalFailed."""
    return {"type": type(e).__name__, "message": str(e)}


def exception_from_wire(data: dict[str, str]) -> Exception:
    """Rebuilds an exception received from the server."""
    name = data.get("type", "")
    message = data.get("message", "")
    cls = WIRE_EXCEPTIONS.get(name)
    if cls is None:
        # Keep the original class name visible; the caller still gets a catchable type
        return RetrievalFailed(f"{name}: {message}" if name else message)
    return cls(message)
