import aiohttp
from ezmm import MultimodalSequence


class RetrievalIntegration:
    """Any integration used to retrieve information via a proprietary API, i.e., where
    direct URL scraping is not possible."""

    domains: list[str]  # The domains supported by this integration

    async def get(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence:
        """Retrieves the contents present at the given URL."""
        raise NotImplementedError
