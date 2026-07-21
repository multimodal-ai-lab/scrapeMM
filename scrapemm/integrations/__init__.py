from typing import Optional

from ezmm import MultimodalSequence

from scrapemm.util import get_domain
from .archive_org import ArchiveOrg
from .bluesky import Bluesky
from .decodo import Decodo, decodo
from .fb import Facebook
from .firecrawl import Firecrawl, fire
from .instagram import Instagram
from .telegram import Telegram
from .tiktok import TikTok
from .x import X
from .youtube import YouTube
from .perma_cc import PermaCC
from .archive_today import ArchiveToday
from .headed_browser import HeadedBrowser
from .ghostarchive import Ghostarchive
from .awesomescreenshot import AwesomeScreenshot

RETRIEVAL_INTEGRATIONS = [
    X(),
    Telegram(),
    Bluesky(),
    TikTok(),
    Instagram(),
    Facebook(),
    YouTube(),
    PermaCC(),
    ArchiveToday(),
    ArchiveOrg(),
    HeadedBrowser(),
    Ghostarchive(),
    AwesomeScreenshot()
]

DOMAIN_TO_INTEGRATION = {
    domain: integration
    for integration in RETRIEVAL_INTEGRATIONS
    for domain in integration.domains
}

NAME_TO_INTEGRATION = {integration.name.lower(): integration for integration in RETRIEVAL_INTEGRATIONS}

INTEGRATION_NAMES = [integration.name for integration in RETRIEVAL_INTEGRATIONS]


async def retrieve_via_integration(url: str, integration_name: str, **kwargs) -> Optional[MultimodalSequence]:
    integration = NAME_TO_INTEGRATION[integration_name.lower()]
    if integration.connected or integration.connected is None:
        return await integration.get(url, **kwargs)


def get_integrations_for_url(url: str) -> list[str]:
    """Returns the list of integration names that support the given domain."""
    domain = get_domain(url)
    return [integration.name
            for integration in RETRIEVAL_INTEGRATIONS
            if domain in integration.domains]
