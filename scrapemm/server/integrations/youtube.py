import logging

from scrapemm.server.integrations.ytdlp import get_content_with_ytdlp
from scrapemm.server.integrations.base import RetrievalIntegration
from scrapemm.common.scraping_response import ScrapedContent

logger = logging.getLogger("scrapeMM")


class YouTube(RetrievalIntegration):
    """YouTube integration for downloading videos and shorts using yt-dlp.
    YouTube is rate-limited to 333 videos per hour."""

    name = "YouTube"
    domains = [
        "youtube.com",
        "youtu.be",
    ]

    async def _connect(self):
        self.connected = True  # Connect always by default

    async def _get(self, url: str, **kwargs) -> ScrapedContent:
        """Downloads YouTube video or short using yt-dlp."""
        sequence = await get_content_with_ytdlp(url, platform="YouTube", **kwargs)
        return ScrapedContent(multimodal=sequence)
