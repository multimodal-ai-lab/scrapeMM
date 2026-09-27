"""Which retrieval methods this deployment wants to use at all.

A method can be perfectly configured and still be one you would rather scrapeMM did not
reach for -- a paid API you are done spending on, a platform whose terms you are
reconsidering, an integration that is misbehaving today. Disabling it takes it out of
retrieval entirely rather than leaving it to fail its way down the method list.

Disabled names are kept in the server config, so the choice survives a restart.
"""

import logging

from scrapemm.common.paths import APP_NAME
from .config import get_config_var, update_config

logger = logging.getLogger(APP_NAME)

CONFIG_KEY = "disabled_methods"

# Former names of methods, by their current one. A method switched off under its old name
# stays off, and requests naming it the old way keep working.
ALIASES = {"headed browser": "browser"}


def resolve_alias(name: str) -> str:
    """The current name of a method that may be named the old way. Leaves every other
    name exactly as it is, case included."""
    return ALIASES.get(str(name).lower(), name)


def _key(name: str) -> str:
    return resolve_alias(name).lower()


def disabled() -> set[str]:
    """The lower-cased names of every disabled integration or method."""
    return {_key(name) for name in (get_config_var(CONFIG_KEY) or [])}


def is_enabled(name: str) -> bool:
    return _key(name) not in disabled()


def set_enabled(name: str, enabled: bool) -> bool:
    """Turns one method on or off. Returns the new state."""
    current = disabled()
    key = _key(name)
    if enabled:
        current.discard(key)
    else:
        current.add(key)
    update_config(**{CONFIG_KEY: sorted(current)})
    logger.info(f"{'Enabled' if enabled else 'Disabled'} retrieval method '{name}'.")
    return enabled


def filter_methods(methods: list[str]) -> list[str]:
    """Drops the disabled methods from a resolved method list."""
    return [method for method in methods if is_enabled(method)]
