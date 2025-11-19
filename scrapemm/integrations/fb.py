import logging
from urllib.parse import urlparse
import aiohttp
from ezmm import MultimodalSequence, download_image

from scrapemm.scraping.ytdlp import check_ytdlp_available,  get_video_with_ytdlp
from scrapemm.integrations.base import RetrievalIntegration
from scrapemm.util import get_domain

logger = logging.getLogger("scrapeMM")


class Facebook(RetrievalIntegration):
    name = "Facebook"
    domains = ["facebook.com", "www.facebook.com"]

    async def _connect(self):
        # Check if yt-dlp is available
        self.ytdlp_available = check_ytdlp_available()

        if self.ytdlp_available:
            self.connected = True
            mode = "yt-dlp only"
            logger.info(f"✅ Facebook integration ready ({mode} mode).")
        else:
            self.connected = False
            logger.warning("❌ Facebook integration not available: Neither API credentials nor yt-dlp found.")

    async def _get(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves content from a Facebook post URL."""
        if get_domain(url) not in self.domains:
            logger.error(f"❌ Invalid domain for Facebook: {get_domain(url)}")
            return None
        
        # Determine if this is a video or profile URL
        if self._is_video_url(url):
            return await self._get_video(url, session)
        elif self._is_photo_url(url):
            return await self._get_photo(url, session)
        else:
            return await self._get_user_profile(url, session)
        
    async def _get_video(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves content from a TikTok video URL."""

        # Fallback to yt-dlp mode
        if self.ytdlp_available:
            return await get_video_with_ytdlp(url, session, platform="Facebook")

        logger.error("❌ No available method to retrieve Facebook video.")
        return None
    
    async def _get_photo(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves content from a Facebook photo URL."""
        logger.error("❌ No available method to retrieve Facebook photo.")
        return None

    async def _get_user_profile(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves content from a Facebook user profile URL."""
        username = self._extract_username(url)
        if username:
            text = f"""**Facebook Profile**
Username: @{username}
URL: {url}

Note: Profile details require Facebook API access.
Configure API credentials for full profile information."""
            return MultimodalSequence([text])
        
        return None

    def _is_video_url(self, url: str) -> bool:
        """Checks if the URL is a Facebook video URL."""
        # video URLS are in the format: https://www.facebook.com/watch?v=VIDEO_ID
        # or Reels: https://www.facebook.com/reel/REEL_ID
        return "facebook.com/watch" in url or "facebook.com/reel" in url
    
    def _extract_video_id(self, url: str) -> str:
        """Extracts the video ID from a Facebook video URL."""
        parsed_url = urlparse(url)
        query_params = parsed_url.query
        for param in query_params.split('&'):
            if param.startswith('v='):
                return param.split('=')[1]
        return ""
    
    def _is_photo_url(self, url: str) -> bool:
        """Checks if the URL is a Facebook photo URL."""
        return "facebook.com/photo" in url or "facebook.com/photos" in url
    
    def _extract_username(self, url: str) -> str:
        """Extracts the username from a Facebook profile URL."""
        # url format: https://www.facebook.com/username<?...>
        parsed_url = urlparse(url)
        path_parts = parsed_url.path.strip('/').split('/')
        if len(path_parts) > 0:
            return path_parts[0]
        return ""
