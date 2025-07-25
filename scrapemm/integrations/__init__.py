import logging

logger = logging.getLogger("scrapeMM")
logger.debug("Starting integrations...")

from typing import Optional

import aiohttp
from ezmm import MultimodalSequence

from scrapemm.util import get_domain
from .bluesky import Bluesky
from .telegram import Telegram
from .tiktok import TikTok
from .x import X
from .fb import Facebook

RETRIEVAL_INTEGRATIONS = [X(), Telegram(), Bluesky(), TikTok(), Facebook()]
DOMAIN_TO_INTEGRATION = {domain: integration
                         for integration in RETRIEVAL_INTEGRATIONS
                         for domain in integration.domains
                         if integration.connected}


async def retrieve_via_integration(url: str, session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
    domain = get_domain(url)
    if domain in DOMAIN_TO_INTEGRATION:
        integration = DOMAIN_TO_INTEGRATION[domain]
        return await integration.get(url, session)
