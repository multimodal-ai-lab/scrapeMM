import logging

from ezmm import MultimodalSequence

from scrapemm.integrations.base import RetrievalIntegration
from scrapemm.integrations.ytdlp import get_content_with_ytdlp

logger = logging.getLogger("scrapeMM")


class ArchiveOrg(RetrievalIntegration):
    """Integration for retrieving content from archive.org.
    TODO: Not working yet, but yt-dlp might support this soon:
    https://github.com/yt-dlp/yt-dlp-master-builds/releases/tag/2025.12.12.222600"""
    name = "Internet Archive"
    domains = ["archive.org"]

    async def _connect(self):
        self.connected = True

    async def _get(self, url: str, **kwargs) -> MultimodalSequence | None:
        return await get_content_with_ytdlp(
            url,
            platform="Internet Archive",
            **kwargs
        )
