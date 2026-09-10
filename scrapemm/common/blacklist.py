"""Persistent blacklist of domains that scrapeMM cannot retrieve, e.g., because
they are protected by a CAPTCHA."""

import logging
import time
from pathlib import Path
from typing import Optional

import yaml

from .paths import APP_NAME, BLACKLIST_PATH

logger = logging.getLogger(APP_NAME)


class DomainBlacklist:
    """Maps each blacklisted domain to the reason why it was blacklisted. Persisted
    to disk, i.e., kept across processes."""

    def __init__(self, path: Path = BLACKLIST_PATH):
        self.path = path
        self._domains: dict[str, str] = self._load()

    def reason(self, domain: str) -> Optional[str]:
        """Returns why the domain was blacklisted (None if it is not blacklisted)."""
        return self._domains.get(domain)

    def add(self, domain: str, reason: str) -> None:
        """Blacklists the domain persistently, remembering the given reason."""
        if self._domains.get(domain) == reason:
            return
        self._domains[domain] = reason
        self._save()
        logger.warning(f"⛔ Blacklisted domain '{domain}': {reason}")

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
        """Returns all blacklisted domains along with the respective reason."""
        return dict(self._domains)

    def __contains__(self, domain: str) -> bool:
        return domain in self._domains

    def __len__(self) -> int:
        return len(self._domains)

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            logger.warning(f"Could not read the domain blacklist at {self.path}.", exc_info=True)
            return {}

    def _save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                yaml.safe_dump(self._domains, f, allow_unicode=True, sort_keys=True)
        except OSError:
            logger.warning(f"Could not write the domain blacklist to {self.path}.", exc_info=True)


blacklist = DomainBlacklist()


def blacklist_domain(domain: str, reason: str) -> None:
    """Blacklists the given domain persistently, i.e., excludes it from any retrieval."""
    blacklist.add(domain, reason)


def unblacklist_domain(domain: str) -> bool:
    """Makes the given domain retrievable again. Returns False if it wasn't blacklisted."""
    return blacklist.remove(domain)


def get_blacklisted_domains() -> dict[str, str]:
    """Returns all blacklisted domains along with the reason for their blacklisting."""
    return blacklist.domains()


def captcha_reason(error: Exception, url: str) -> str:
    """Composes the blacklisting reason for a domain that is protected by a CAPTCHA."""
    return f"{error} URL: {url}. Detected on {time.strftime('%Y-%m-%d %H:%M')}."
