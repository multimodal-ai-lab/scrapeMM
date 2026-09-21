import logging
import re
from urllib.parse import urlparse

import aiohttp
from ezmm import MultimodalSequence

from scrapemm import RetrievalFailed
from scrapemm.common.exceptions import RateLimitError, TargetUnavailableError, AccessBlockedError
from scrapemm.common.retrieval_integration import RetrievalIntegration
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.integrations.ytdlp import get_content_with_ytdlp
from scrapemm.secrets import get_secret
from scrapemm.util import to_scraped_content
from ..common import CONFIG_DIR

logger = logging.getLogger("scrapeMM")

SHORTCODE_REGEX = re.compile(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")
EMBED_URL = "https://www.instagram.com/p/{shortcode}/embed/captioned/"
# The embed page renders the post server-side. Instagram drops this marker on the
# post's medium, so its absence means the embed carries no post (deleted, private,
# or age-restricted).
EMBED_MEDIA_MARKER = "EmbeddedMediaImage"


class Instagram(RetrievalIntegration):
    name = "Instagram"
    domains = ["instagram.com", "www.instagram.com"]
    cookie_file = CONFIG_DIR / "instagram_cookie.txt"

    async def _connect(self):
        self.api_available = False

        cookie = get_secret("instagram_cookie")
        if cookie:
            # Save the cookie in a .txt file next to the secrets file
            with open(self.cookie_file, "w") as f:
                f.write(cookie)
            logger.info("✅ Using cookie to connect to Instagram.")
        else:
            logger.warning("⚠️ Missing Instagram cookie. Won't be able to download "
                           "age-restricted content.")

        logger.info(f"✅ Instagram integration ready (yt-dlp only mode).")
        self.connected = True

    async def _get(self, url: str, **kwargs) -> ScrapedContent:
        """Retrieves content from an Instagram post URL."""
        # Determine if this is a video or profile URL
        if self._is_video_url(url):
            return await self._get_video(url, **kwargs)
        elif self._is_photo_url(url):
            # /p/ URLs can also be reels, so try both
            content = None
            try:
                content = await self._get_video(url, **kwargs)
            except (TargetUnavailableError, RetrievalFailed, AccessBlockedError):
                pass  # Uncritical errors that can be ignored
            except RateLimitError:
                raise
            except Exception:
                # Needs closer investigation
                logger.debug(f"Instagram video retrieval failed for {url}", exc_info=True)

            if content and content.multimodal.has_videos():
                return content
            else:
                return await self._get_photo(url, **kwargs)
        else:
            return await self._get_user_profile(url, **kwargs)

    async def _get_video(self, url: str, **kwargs) -> ScrapedContent:
        """Retrieves content from an Instagram video URL."""
        if self.api_available:
            raise NotImplementedError
        else:
            # Age-restricted posts ("can't be seen by certain audiences") are only
            # served to a logged-in session.
            cookie_file_path = self.cookie_file.as_posix() if get_secret("instagram_cookie") else None
            sequence = await get_content_with_ytdlp(url, platform="Instagram",
                                                    cookiefile=cookie_file_path, **kwargs)
            return ScrapedContent(multimodal=sequence)

    async def _get_photo(self, url: str, **kwargs) -> ScrapedContent:
        """Retrieves content from an Instagram photo URL (can also be a reel)."""
        content = await self._get_photo_from_embed(url, **kwargs)
        if content:
            return content

        # The embed is unavailable, so scrape the post page itself. Instagram serves
        # it as an empty shell, so this needs a proxy rendering the JavaScript, which
        # takes tens of seconds.
        from scrapemm.integrations.decodo import decodo
        return await decodo.scrape(url, session=kwargs["session"], timeout=60,
                                   output_format=kwargs.get("output_format", "multimodal"))

    async def _get_photo_from_embed(self, url: str, **kwargs) -> ScrapedContent | None:
        """Reads the post off Instagram's embed page, which serves the post's medium
        and caption rendered server-side and without login. Returns None if that page
        does not carry the post, leaving it to the caller to fall back.

        Worth the try before anything else: the embed answers within a second, while
        rendering the post page itself through a proxy takes 30 to 50 seconds."""
        match = SHORTCODE_REGEX.search(url)
        if not match:
            return None

        session = kwargs["session"]
        embed_url = EMBED_URL.format(shortcode=match.group(1))
        try:
            async with session.get(
                    embed_url, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status != 200:
                    logger.debug(f"Instagram embed of {url} returned "
                                 f"status {response.status}.")
                    return None
                html = await response.text()
        except Exception:
            logger.debug(f"Could not fetch the Instagram embed of {url}.", exc_info=True)
            return None

        if EMBED_MEDIA_MARKER not in html:
            logger.debug(f"Instagram embed of {url} carries no post.")
            return None

        return await to_scraped_content(
            html, session=session, url=embed_url,
            output_format=kwargs.get("output_format", "multimodal"),
            max_video_size=kwargs.get("max_video_size"),
        )

    async def _get_user_profile(self, url: str, **kwargs) -> ScrapedContent:
        """Retrieves content from an Instagram user profile URL."""
        username = self._extract_username(url)
        raise NotImplementedError(f"No method available to retrieve Instagram profiles (user @{username}).")

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
