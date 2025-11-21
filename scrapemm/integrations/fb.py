import logging
import re
from urllib.parse import urlparse

import aiohttp
from ezmm import MultimodalSequence

from scrapemm.integrations.base import RetrievalIntegration
from scrapemm.scraping.ytdlp import get_content_with_ytdlp
from scrapemm.util import get_domain

logger = logging.getLogger("scrapeMM")

VIDEO_URL_REGEX = r"facebook\.com/\d+/videos/\d+/?"


class Facebook(RetrievalIntegration):
    name = "Facebook"
    domains = ["facebook.com", "www.facebook.com"]

    async def _connect(self):
        self.api_available = False  # TODO
        logger.info(f"✅ Facebook integration ready (yt-dlp only mode).")
        self.connected = True

    async def _get(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves content from a Facebook post URL."""
        if get_domain(url) not in self.domains:
            logger.error(f"❌ Invalid domain for Facebook: {get_domain(url)}")
            return None

        # Determine if this is a video or photo URL, act accordingly
        if self._is_video_url(url):
            return await self._get_video(url, session)
        elif self._is_photo_url(url):
            return await self._get_photo(url, session)

        # The URL is not indicative, so try all methods
        return (await self._get_video(url, session) or
                await self._get_photo(url, session) or
                await self._get_user_profile(url, session))

    async def _get_video(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves content from a Facebook video URL."""
        if self.api_available:
            raise NotImplementedError
        else:
            return await get_content_with_ytdlp(url, session, platform="Facebook")

    async def _get_photo(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves content from a Facebook photo URL."""
        logger.error("❌ No available method to retrieve Facebook photo.")
        return None

    async def _get_user_profile(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves content from a Facebook user profile URL."""
        logger.error("❌ No available method to retrieve Facebook profiles.")
        return None

    def _is_video_url(self, url: str) -> bool:
        """Checks if the URL is a Facebook video URL."""
        # video URLS are in the format: https://www.facebook.com/watch?v=VIDEO_ID
        # or Reels: https://www.facebook.com/reel/REEL_ID
        return "facebook.com/watch" in url or "facebook.com/reel" in url or bool(re.search(VIDEO_URL_REGEX, url))

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
