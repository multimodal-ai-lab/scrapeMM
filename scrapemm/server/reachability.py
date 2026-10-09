"""A cheap verdict on whether a URL's host is reachable at all, shared across URLs.

A host that is down made every one of its URLs cycle through all retrieval methods,
each waiting for its full timeout: 3 to 5 minutes per URL (staging.vishvasnews.com,
five URLs in one benchmark). One DNS lookup and one TCP connect settle it in seconds,
and the verdict is cached per host, so the host's other URLs do not ask again.

A host can also answer, and send every URL on to a host that does not exist: the
`www.` form of malayalam.factcrescendo.com redirects to
`https://malayalam.factcrescendo.comslug`, the slash lost. Every method then failed on
a DNS error after its own timeout, and the archives were asked last. One request that
does not follow redirects finds that out (see `redirect_rewrite()`).
"""
import asyncio
import ipaddress
import logging
import os
import socket
import time
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

logger = logging.getLogger("scrapeMM")

Verdict = Literal["ok", "unreachable", "dead"]
# ok: nothing speaks against the host.
# unreachable: resolves, but refuses or ignores TCP connections from this server.
#   Methods running here cannot load it; remote services (other IPs) still may.
# dead: does not resolve (NXDOMAIN), or remote services could not reach it either.

CONNECT_TIMEOUT = 10  # Healthy hosts connect in well under a second
TTL = 300  # Long enough to cover a batch, short enough for a host that comes back

_verdicts: dict[tuple[str, int], tuple[Verdict, str, float]] = {}
_probes: dict[tuple[str, int], asyncio.Task] = {}

# Chromium's net errors that mean the host could not be reached at all, as opposed to
# a page that is merely slow
_NETWORK_FAILURES = ("err_name_not_resolved", "err_connection_refused", "err_address_unreachable",
                     "err_connection_timed_out", "err_internet_disconnected", "err_name_resolution_failed")


def is_network_failure(exc: BaseException) -> bool:
    """Whether a browser navigation error says the host is unreachable (DNS, refused,
    unreachable), in which case retrying the navigation is pointless."""
    return any(marker in str(exc).lower() for marker in _NETWORK_FAILURES)


def _target(url: str) -> Optional[tuple[str, int]]:
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        return parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None


def _behind_proxy() -> bool:
    """With an outgoing proxy, a direct connect says nothing about what the methods see."""
    return any(os.environ.get(v) for v in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY",
                                           "http_proxy", "ALL_PROXY", "all_proxy"))


async def check(url: str) -> tuple[Verdict, str]:
    """The verdict on the URL's host along with the reason for it. Concurrent calls for
    the same host share one probe, and verdicts are cached for `TTL` seconds."""
    target = _target(url)
    if target is None or _behind_proxy():
        return "ok", ""
    redirect = _redirects.get(target)
    if redirect and redirect.dead and redirect.expires > time.monotonic():
        return "dead", redirect.dead
    cached = _verdicts.get(target)
    if cached and cached[2] > time.monotonic():
        return cached[0], cached[1]

    probe = _probes.get(target)
    if probe is None or probe.get_loop() is not asyncio.get_running_loop():
        probe = asyncio.ensure_future(_probe(*target))
        _probes[target] = probe
        probe.add_done_callback(lambda _, t=target: _probes.pop(t, None))
    # Shielded: one URL giving up must not cancel the probe the others wait for
    verdict, reason = await asyncio.shield(probe)
    _verdicts[target] = (verdict, reason, time.monotonic() + TTL)
    return verdict, reason


REDIRECT_PROBE_TIMEOUT = 5  # Seconds for the one request that looks at the redirect
REDIRECT_STATUSES = (301, 302, 303, 307, 308)


@dataclass
class _Redirect:
    """What a host's redirect (the one its URLs get) comes to."""
    rewrite_host: Optional[str]  # The host its URLs belong on, if the redirect only lost a slash
    dead: Optional[str]  # Why they lead nowhere, if they do
    expires: float


_redirects: dict[tuple[str, int], _Redirect] = {}


async def redirect_rewrite(url: str, session: aiohttp.ClientSession,
                           headers: Optional[dict] = None) -> Optional[str]:
    """For a URL whose host redirects to a host that does not exist: the URL on the host
    the redirect meant, if it only lost the slash between host and path (it sent
    `https://example.comslug` for `https://example.com/slug`). None if the URL is fine,
    or if there is nothing to repair: then the host is recorded as dead (see `check()`),
    so that the methods are not all sent after it in turn.

    One request that does not follow redirects, for the first URL of a host; the answer
    holds for the host's other URLs for `TTL` seconds (a redirect of a whole host, such
    as www. to the bare domain, is the same for all its paths)."""
    target = _target(url)
    if target is None or _behind_proxy():
        return None
    rule = _redirects.get(target)
    if rule is None or rule.expires <= time.monotonic():
        rule = await _probe_redirect(url, target, session, headers)
        _redirects[target] = rule
    if not rule.rewrite_host:
        return None
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(parts._replace(netloc=rule.rewrite_host + port))


async def _probe_redirect(url: str, target: tuple[str, int], session: aiohttp.ClientSession,
                          headers: Optional[dict]) -> _Redirect:
    unremarkable = _Redirect(None, None, time.monotonic() + TTL)
    try:
        async with session.get(url, headers=headers, allow_redirects=False,
                               timeout=aiohttp.ClientTimeout(total=REDIRECT_PROBE_TIMEOUT)) as response:
            status, location = response.status, response.headers.get("Location")
    except Exception:
        # No verdict on that; the methods will find out for themselves. Asked again soon
        return _Redirect(None, None, time.monotonic() + 30)
    if status not in REDIRECT_STATUSES or not location:
        return unremarkable

    host = target[0]
    sent_to = (urlsplit(urljoin(url, location)).hostname or "").lower()
    if not sent_to or sent_to == host or await _resolves(sent_to) is not False:
        return unremarkable  # Elsewhere, but somewhere that exists

    meant = _meant_host(host, sent_to)
    if meant and await _resolves(meant):
        logger.info(f"🔧 {host} redirects to {sent_to}, which does not exist: its URLs are "
                    f"retrieved from {meant}.")
        return _Redirect(meant, None, time.monotonic() + TTL)
    reason = f"{host} redirects to {sent_to}, a host that does not exist."
    logger.info(f"🔌 {reason}")
    return _Redirect(None, reason, time.monotonic() + TTL)


def _meant_host(host: str, sent_to: str) -> Optional[str]:
    """The host a redirect meant when it lost the slash after it: the URL's own host
    (without its www.) is the start of the host it was sent to, the path following
    directly (`malayalam.factcrescendo.com` + `old-image-...`)."""
    base = host[4:] if host.startswith("www.") else host
    if (sent_to.startswith(base) and len(sent_to) > len(base)
            and sent_to[len(base)] not in ".:"):
        return base
    return None


async def _resolves(host: str) -> Optional[bool]:
    """Whether the name resolves: False if it does not exist (or is no valid host name),
    None if the resolver merely had a hiccup."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, 443, type=socket.SOCK_STREAM), CONNECT_TIMEOUT)
        return True
    except socket.gaierror as e:
        if e.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", None)):
            return False
        return None
    except UnicodeError:  # A label of more than 63 characters, say
        return False
    except (OSError, asyncio.TimeoutError):
        return None


def mark_dead(url: str, reason: str) -> None:
    """Records that remote services could not reach the host either."""
    if target := _target(url):
        _verdicts[target] = ("dead", reason, time.monotonic() + TTL)


async def _probe(host: str, port: int) -> tuple[Verdict, str]:
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM), CONNECT_TIMEOUT)
            addresses = list(dict.fromkeys(info[4][0] for info in infos))
        except socket.gaierror as e:
            if e.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", None)):
                logger.info(f"🔌 {host} does not resolve (DNS: {e.strerror}).")
                return "dead", f"The host {host} does not exist (DNS lookup failed: {e.strerror})."
            return "ok", ""  # A resolver hiccup, not a verdict on the host
        except (OSError, asyncio.TimeoutError):
            return "ok", ""

    started = time.monotonic()
    errors = []
    for address in addresses[:4]:  # Enough to cover IPv4 and IPv6
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(address, port),
                                               CONNECT_TIMEOUT)
            writer.close()
            return "ok", ""
        except asyncio.TimeoutError:
            errors.append(f"{address}: no answer within {CONNECT_TIMEOUT} s")
        except OSError as e:
            errors.append(f"{address}: {e.strerror or type(e).__name__}")
        if time.monotonic() - started > 2 * CONNECT_TIMEOUT:
            break
    reason = f"The host {host} does not accept connections on port {port} ({'; '.join(errors)})."
    logger.info(f"🔌 {reason}")
    return "unreachable", reason
