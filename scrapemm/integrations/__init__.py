from typing import Optional

from ezmm import MultimodalSequence

from scrapemm.util import get_domain
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

RETRIEVAL_INTEGRATIONS = [X(), Telegram(), Bluesky(), TikTok(), Instagram(), Facebook(), YouTube(), PermaCC()]
DOMAIN_TO_INTEGRATION = {domain: integration
                         for integration in RETRIEVAL_INTEGRATIONS
                         for domain in integration.domains}


async def retrieve_via_integration(url: str, **kwargs) -> Optional[MultimodalSequence]:
    domain = get_domain(url)
    if domain in DOMAIN_TO_INTEGRATION:
        integration = DOMAIN_TO_INTEGRATION[domain]
        if integration.connected or integration.connected is None:
            return await integration.get(url, **kwargs)
