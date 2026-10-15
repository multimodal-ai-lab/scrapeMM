import logging

from scrapemm.server.integrations.ytdlp import (get_content_with_ytdlp, get_youtube_listing,
                                                is_youtube_listing)
from scrapemm.server.integrations.base import RetrievalIntegration
from scrapemm.common.scraping_response import ScrapedContent

logger = logging.getLogger("scrapeMM")


class YouTube(RetrievalIntegration):
    """YouTube integration for downloading videos and shorts using yt-dlp, and listing
    channels and playlists (their latest videos with thumbnails, not streams).
    YouTube is rate-limited to 333 videos per hour."""

    name = "YouTube"
    post_media = True
    domains = [
        "youtube.com",
        "youtu.be",
    ]

    async def _connect(self):
        self.connected = True  # Connect always by default

    async def _get(self, url: str, **kwargs) -> ScrapedContent:
        """Downloads YouTube video or short using yt-dlp; lists a channel or playlist."""
        if is_youtube_listing(url):
            return ScrapedContent(multimodal=await get_youtube_listing(url, kwargs.get("session")))
        return await get_content_with_ytdlp(url, platform="YouTube", **kwargs)
