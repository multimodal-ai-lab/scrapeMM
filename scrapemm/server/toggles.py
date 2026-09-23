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


def disabled() -> set[str]:
    """The lower-cased names of every disabled integration or method."""
    return {str(name).lower() for name in (get_config_var(CONFIG_KEY) or [])}


def is_enabled(name: str) -> bool:
    return name.lower() not in disabled()


def set_enabled(name: str, enabled: bool) -> bool:
    """Turns one method on or off. Returns the new state."""
    current = disabled()
    key = name.lower()
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
