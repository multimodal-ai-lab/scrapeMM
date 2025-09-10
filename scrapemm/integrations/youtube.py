import logging
from typing import Optional

import aiohttp
from ezmm import MultimodalSequence

from scrapemm.scraping.ytdlp import get_video_with_ytdlp, check_ytdlp_available
from .base import RetrievalIntegration

logger = logging.getLogger("scrapeMM")


class YouTube(RetrievalIntegration):
    """YouTube integration for downloading videos and shorts using yt-dlp."""
    
    domains = [
        "youtube.com", 
        "www.youtube.com", 
        "youtu.be", 
        "m.youtube.com"
    ]
    
    def __init__(self):
        self.connected = check_ytdlp_available()
        if not self.connected:
            logger.warning("❌ YouTube integration disabled: yt-dlp not available")
        else:
            logger.info("✅ YouTube integration enabled")

    async def get(self, url: str, session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
        """Downloads YouTube video or short using yt-dlp."""
        if not self.connected:
            logger.error("❌ YouTube integration not connected")
            return None
            
        logger.info(f"📺 Downloading YouTube content: {url}")
        return await get_video_with_ytdlp(url, session, "YouTube")
