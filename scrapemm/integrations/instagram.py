import logging
from urllib.parse import urlparse

import aiohttp
from ezmm import MultimodalSequence

from scrapemm.integrations.base import RetrievalIntegration
from scrapemm.scraping.ytdlp import get_content_with_ytdlp
from scrapemm.util import get_domain

logger = logging.getLogger("scrapeMM")


class Instagram(RetrievalIntegration):
    name = "Instagram"
    domains = ["instagram.com", "www.instagram.com"]

    async def _connect(self):
        self.api_available = False
        logger.info(f"✅ Instagram integration ready (yt-dlp only mode).")
        self.connected = True

    async def _get(self, url: str, **kwargs) -> MultimodalSequence | None:
        """Retrieves content from an Instagram post URL."""
        if get_domain(url) not in self.domains:
            logger.error(f"❌ Invalid domain for Instagram: {get_domain(url)}")
            return None

        # Determine if this is a video or profile URL
        if self._is_video_url(url):
            return await self._get_video(url, **kwargs)
        elif self._is_photo_url(url):
            # /p/ URLs can also be reels, so try both
            content = await self._get_video(url, **kwargs)
            if content and content.has_videos():
                return content
            else:
                return await self._get_photo(url, **kwargs)
        else:
            return await self._get_user_profile(url, **kwargs)

    async def _get_video(self, url: str, **kwargs) -> MultimodalSequence | None:
        """Retrieves content from an Instagram video URL."""
        if self.api_available:
            raise NotImplementedError
        else:
            return await get_content_with_ytdlp(url, platform="Instagram", **kwargs)

    async def _get_photo(self, url: str, **kwargs) -> MultimodalSequence | None:
        """Retrieves content from an Instagram photo URL (can also be a reel)."""
        logger.warning("❌ Native Instagram photo download not yet supported. Use Decodo for that.")
        return None

    async def _get_user_profile(self, url: str, **kwargs) -> MultimodalSequence | None:
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
