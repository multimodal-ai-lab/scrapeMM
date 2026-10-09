"""A cheap verdict on whether a URL's host is reachable at all, shared across URLs.

A host that is down made every one of its URLs cycle through all retrieval methods,
each waiting for its full timeout: 3 to 5 minutes per URL (staging.vishvasnews.com,
five URLs in one benchmark). One DNS lookup and one TCP connect settle it in seconds,
and the verdict is cached per host, so the host's other URLs do not ask again.
"""
import asyncio
import ipaddress
import logging
import os
import socket
import time
from typing import Literal, Optional
from urllib.parse import urlsplit

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
