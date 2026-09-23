"""Persistent blacklist of domains that scrapeMM cannot retrieve, e.g., because
they are protected by a CAPTCHA."""

import logging
import time
from pathlib import Path
from typing import Optional

import yaml

from scrapemm.common.paths import APP_NAME
from .paths import BLACKLIST_PATH

logger = logging.getLogger(APP_NAME)

# Automatic (CAPTCHA-triggered) blacklistings expire after this many seconds. CAPTCHA
# gates are frequently transient -- a burst of parallel requests can trigger one on a
# domain that is perfectly retrievable an hour later -- so excluding a domain forever
# would silently erode coverage over time.
DEFAULT_BLACKLIST_TTL = 7 * 24 * 60 * 60  # 7 days


class DomainBlacklist:
    """Maps each blacklisted domain to the reason why it was blacklisted. Persisted
    to disk, i.e., kept across processes.

    Entries added automatically (because a CAPTCHA was encountered) expire after
    `ttl` seconds. Entries added manually via `blacklist_domain()` are permanent:
    they express a deliberate decision, not an observation that may go stale.
    """

    def __init__(self, path: Path = BLACKLIST_PATH, ttl: float = DEFAULT_BLACKLIST_TTL):
        self.path = path
        self.ttl = ttl
        self._domains: dict[str, dict] = self._load()

    def reason(self, domain: str) -> Optional[str]:
        """Returns why the domain was blacklisted (None if it is not blacklisted).
        Expired entries are dropped and count as not blacklisted."""
        entry = self._domains.get(domain)
        if entry is None:
            return None
        if self._is_expired(entry):
            del self._domains[domain]
            self._save()
            logger.info(f"⌛ Blacklisting of '{domain}' expired. Retrieving it again.")
            return None
        return entry["reason"]

    def add(self, domain: str, reason: str, permanent: bool = False) -> None:
        """Blacklists the domain persistently, remembering the given reason.
        Unless `permanent`, the entry expires after `self.ttl` seconds."""
        existing = self._domains.get(domain)
        if existing and existing["reason"] == reason and existing["permanent"] == permanent:
            return
        self._domains[domain] = dict(reason=reason, added=time.time(), permanent=permanent)
        self._save()
        expiry = "permanently" if permanent else f"for {self.ttl / 86400:.1f} days"
        logger.warning(f"⛔ Blacklisted domain '{domain}' {expiry}: {reason}")

    def remove(self, domain: str) -> bool:
        """Removes the domain from the blacklist. Returns False if it wasn't blacklisted."""
        if self._domains.pop(domain, None) is None:
            return False
        self._save()
        logger.info(f"Removed '{domain}' from the blacklist.")
        return True

    def clear(self) -> None:
        """Empties the blacklist."""
        self._domains.clear()
        self._save()

    def domains(self) -> dict[str, str]:
        """Returns all non-expired blacklisted domains along with the respective reason."""
        self._prune()
        return {domain: entry["reason"] for domain, entry in self._domains.items()}

    def _is_expired(self, entry: dict) -> bool:
        if entry["permanent"] or self.ttl <= 0:
            return False
        return time.time() - entry["added"] > self.ttl

    def _prune(self) -> None:
        """Drops all expired entries."""
        alive = {d: e for d, e in self._domains.items() if not self._is_expired(e)}
        if len(alive) != len(self._domains):
            self._domains = alive
            self._save()

    def __contains__(self, domain: str) -> bool:
        return self.reason(domain) is not None

    def __len__(self) -> int:
        self._prune()
        return len(self._domains)

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            logger.warning(f"Could not read the domain blacklist at {self.path}.", exc_info=True)
            return {}
        return {domain: self._parse_entry(value) for domain, value in raw.items()}

    @staticmethod
    def _parse_entry(value) -> dict:
        """Reads one blacklist entry, accepting the legacy format where an entry was
        just the reason string. Legacy entries start their TTL now, i.e. they are
        given a fresh window rather than expiring immediately."""
        if isinstance(value, dict):
            return dict(reason=value.get("reason", ""),
                        added=value.get("added", time.time()),
                        permanent=value.get("permanent", False))
        return dict(reason=str(value), added=time.time(), permanent=False)

    def _save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                yaml.safe_dump(self._domains, f, allow_unicode=True, sort_keys=True)
        except OSError:
            logger.warning(f"Could not write the domain blacklist to {self.path}.", exc_info=True)


blacklist = DomainBlacklist()


def set_blacklist_ttl(seconds: float) -> None:
    """Sets how long (in seconds) an automatically blacklisted domain stays excluded.
    Use 0 (or any negative value) to keep automatic blacklistings forever."""
    blacklist.ttl = seconds


def blacklist_domain(domain: str, reason: str) -> None:
    """Blacklists the given domain permanently, i.e., excludes it from any retrieval
    until `unblacklist_domain()` is called."""
    blacklist.add(domain, reason, permanent=True)


def unblacklist_domain(domain: str) -> bool:
    """Makes the given domain retrievable again. Returns False if it wasn't blacklisted."""
    return blacklist.remove(domain)


def get_blacklisted_domains() -> dict[str, str]:
    """Returns all blacklisted domains along with the reason for their blacklisting."""
    return blacklist.domains()


def captcha_reason(error: Exception, url: str) -> str:
    """Composes the blacklisting reason for a domain that is protected by a CAPTCHA."""
    return f"{error} URL: {url}. Detected on {time.strftime('%Y-%m-%d %H:%M')}."
