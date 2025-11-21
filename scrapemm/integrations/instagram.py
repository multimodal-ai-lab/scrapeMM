import logging
from urllib.parse import urlparse
import aiohttp
from ezmm import MultimodalSequence, download_image

from scrapemm.scraping.ytdlp import check_ytdlp_available,  get_video_with_ytdlp
from scrapemm.integrations.base import RetrievalIntegration
from scrapemm.util import get_domain

logger = logging.getLogger("scrapeMM")


class Instagram(RetrievalIntegration):
    name = "Instagram"
    domains = ["instagram.com", "www.instagram.com"]

    async def _connect(self):
        # Check if yt-dlp is available
        self.ytdlp_available = check_ytdlp_available()

        if self.ytdlp_available:
            self.connected = True
            mode = "yt-dlp only"
            logger.info(f"✅ Instagram integration ready ({mode} mode).")
        else:
            self.connected = False
            logger.warning("❌ Instagram integration not available: Neither API credentials nor yt-dlp found.")

    async def _get(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves content from an Instagram post URL."""
        if get_domain(url) not in self.domains:
            logger.error(f"❌ Invalid domain for Instagram: {get_domain(url)}")
            return None
        
        # Determine if this is a video or profile URL
        if self._is_video_url(url):
            return await self._get_video(url, session)
        elif self._is_photo_url(url):
            return await self._get_photo(url, session)
        else:
            return await self._get_user_profile(url, session)
        
    async def _get_video(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves content from an Instagram video URL."""
        if self.ytdlp_available:
            return await get_video_with_ytdlp(url, session, platform="Instagram")

        logger.error("❌ No available method to retrieve Instagram video.")
        return None
    
    async def _get_photo(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves content from an Instagram photo URL (can also be a reel)."""
        # Try yt-dlp first since /p/ URLs can be either photos or reels
        if self.ytdlp_available:
            return await get_video_with_ytdlp(url, session, platform="Instagram")

        logger.error("❌ No available method to retrieve Instagram photo.")
        return None
    
    async def _get_user_profile(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves content from an Instagram user profile URL."""
        username = self._extract_username(url)
        if username:
            text = f"""**Instagram Profile**
Username: @{username}
URL: {url}

Note: Profile details require Instagram API access.
Configure API credentials for full profile information."""
            return MultimodalSequence([text])

        return None

    def _is_video_url(self, url: str) -> bool:
        """Checks if the URL is an Instagram video URL."""
        return "instagram.com/reels" in url or "instagram.com/reel/" in url

    def _is_photo_url(self, url: str) -> bool:
        """Checks if the URL is an Instagram photo URL."""
        return "instagram.com/p/" in url

    def _extract_username(self, url: str) -> str:
        """Extracts the username from an Instagram profile URL."""
        parsed_url = urlparse(url)
        path_parts = parsed_url.path.strip('/').split('/')
        if len(path_parts) > 0:
            return path_parts[0]
        return ""
