"""The search providers this server offers. Each gets its own route, POST
/v1/search/<name>; see `scrapemm.search` for how to add one."""

from typing import Optional

from .base import ProviderDisabled, ProviderNotConfigured, SearchProvider
from .serper import Serper

SEARCH_PROVIDERS: list[SearchProvider] = [Serper()]

NAME_TO_PROVIDER = {provider.name: provider for provider in SEARCH_PROVIDERS}


def get_provider(name: str) -> Optional[SearchProvider]:
    return NAME_TO_PROVIDER.get(name.lower())


async def close_sessions() -> None:
    """Closes every provider's HTTP session, on shutdown."""
    for provider in SEARCH_PROVIDERS:
        await provider.close()


__all__ = ["SEARCH_PROVIDERS", "NAME_TO_PROVIDER", "get_provider", "close_sessions", "SearchProvider",
           "ProviderNotConfigured", "ProviderDisabled"]
