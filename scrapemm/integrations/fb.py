import logging
import re
from urllib.parse import urlparse, parse_qs

from ezmm import MultimodalSequence

from scrapemm import RateLimitError
from scrapemm.common import CONFIG_DIR
from scrapemm.common.exceptions import ContentBlockedError
from scrapemm.integrations.base import RetrievalIntegration
from scrapemm.integrations.ytdlp import get_content_with_ytdlp
from scrapemm.secrets import get_secret

logger = logging.getLogger("scrapeMM")

VIDEO_URL_REGEX = r"facebook\.com/\d+/videos/\d+/?"


class Facebook(RetrievalIntegration):
    name = "Facebook"
    domains = ["facebook.com", "fb.watch"]
    cookie_file = CONFIG_DIR / "facebook_cookie.txt"

    async def _connect(self):
        self.api_available = False  # TODO

        cookie = get_secret("facebook_cookie")
        if cookie:
            # Save the cookie in a .txt file next to the secrets file
            with open(self.cookie_file, "w") as f:
                f.write(cookie)
            logger.info(f"✅ Using cookie to connect to Facebook.")
        else:
            logger.warning(f"⚠️ Missing Facebook cookie. Won't be able to download videos that require login.")

        logger.info(f"✅ Facebook integration ready (yt-dlp only mode).")
        self.connected = True

    async def _get(self, url: str, **kwargs) -> MultimodalSequence | None:
        """Retrieves content from a Facebook post URL."""
        url = self._normalize_url(url)

        # Determine if this is a video or photo URL, act accordingly
        if self._is_video_url(url):
            try:
                return await self._get_video(url, **kwargs)
            except Exception as e:
                if "No video formats found" in str(e):
                    raise ContentBlockedError(f"Video is blocked by Facebook.")
                elif "This video is only available for registered users" in str(e):
                    raise RateLimitError(f"Facebook is rate-limiting your IP address. Set a 'facebook_cookie' in ScrapeMM.")
                else:
                    raise e
        elif self._is_photo_url(url):
            return await self._get_photo(url, **kwargs)

        # The URL is not indicative, so try all methods
        return (await self._get_video(url, **kwargs) or
                await self._get_photo(url, **kwargs) or
                await self._get_user_profile(url, **kwargs))

    async def _get_video(self, url: str, **kwargs) -> MultimodalSequence | None:
        """Retrieves content from a Facebook video URL."""
        if self.api_available:
            raise NotImplementedError("Facebook video retrieval through API not yet supported.")
        else:
            return await get_content_with_ytdlp(url,
                                                platform="Facebook",
                                                cookie_file=self.cookie_file.as_posix(),
                                                **kwargs)

    async def _get_photo(self, url: str, **kwargs) -> MultimodalSequence | None:
        """Retrieves content from a Facebook photo URL."""
        raise NotImplementedError("No available method to retrieve Facebook photos.")

    async def _get_user_profile(self, url: str, **kwargs) -> MultimodalSequence | None:
        """Retrieves content from a Facebook user profile URL."""
        raise NotImplementedError("No available method to retrieve Facebook profiles.")

    def _normalize_url(self, url: str) -> str:
        """If the URL is a login Facebook URL, i.e., of the form https://www.facebook.com/login/?next=...
        or https://www.facebook.com/plugins/post.php?href=..., extracts the actual post's URL."""
        if url.startswith("https://www.facebook.com/login/?next="):  # Login redirect URLs
            query = urlparse(url).query
            return parse_qs(query).get("next", [])[0] or url
        elif url.startswith("https://www.facebook.com/plugins/post.php?href="):  # Post embedding links
            query = urlparse(url).query
            return parse_qs(query).get("href", [])[0] or url
        return url

    def _is_video_url(self, url: str) -> bool:
        """Checks if the URL is a Facebook video URL."""
        # video URLS are in the format: https://www.facebook.com/watch?v=VIDEO_ID or fb.watch/...
        # or Reels: https://www.facebook.com/reel/REEL_ID
        return ("facebook.com/watch" in url
                or "facebook.com/reel" in url
                or bool(re.search(VIDEO_URL_REGEX, url))
                or "fb.watch" in url)

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
