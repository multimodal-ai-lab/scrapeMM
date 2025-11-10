from abc import ABC, abstractmethod
from typing import Optional

import aiohttp
from ezmm import MultimodalSequence

from scrapemm.common import logger
from scrapemm.util import get_domain


class RetrievalIntegration(ABC):
    """Any integration used to retrieve information via an external API. Typically
     used when direct URL scraping is not possible."""

    name: str
    domains: list[str]  # The domains supported by this integration
    connected: bool = None

    @abstractmethod
    async def _connect(self):
        """Establish a connection to the external service. Invoked upon the first get() call.
        Must set self.connet = True if connection was successful, else False."""
        raise NotImplementedError

    async def get(self, url: str, session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
        """Ensures connectivity before invoking the service for retrieval."""
        assert get_domain(url) in self.domains
        if self.connected is None:
            await self._connect()
            if not self.connected:
                logger.warning(f"❌ Connection to {self.name} service could not be established.")
        if self.connected:
            logger.debug(f"Calling {self.name} service for {url}")
            return await self._get(url, session)

    @abstractmethod
    async def _get(self, url: str, session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
        """Retrieves the contents present at the given URL."""
        raise NotImplementedError
