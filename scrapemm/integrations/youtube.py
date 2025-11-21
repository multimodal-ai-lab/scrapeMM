import logging
from typing import Optional

import aiohttp
from ezmm import MultimodalSequence

from scrapemm.scraping.ytdlp import get_content_with_ytdlp
from .base import RetrievalIntegration

logger = logging.getLogger("scrapeMM")


class YouTube(RetrievalIntegration):
    """YouTube integration for downloading videos and shorts using yt-dlp."""

    name = "YouTube"
    domains = [
        "youtube.com",
        "youtu.be",
    ]

    async def _connect(self):
        self.connected = True

    async def _get(self, url: str, session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
        """Downloads YouTube video or short using yt-dlp."""
        logger.debug(f"📺 Downloading YouTube content: {url}")
        return await get_content_with_ytdlp(url, session, "YouTube")
