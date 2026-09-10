import logging
from urllib.parse import urlparse

from ezmm import MultimodalSequence

from scrapemm import RetrievalFailed
from scrapemm.common.exceptions import RateLimitError, TargetUnavailableError, AccessBlockedError
from scrapemm.common.retrieval_integration import RetrievalIntegration
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.integrations.ytdlp import get_content_with_ytdlp

logger = logging.getLogger("scrapeMM")


class Instagram(RetrievalIntegration):
    name = "Instagram"
    domains = ["instagram.com", "www.instagram.com"]

    async def _connect(self):
        self.api_available = False
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
            sequence = await get_content_with_ytdlp(url, platform="Instagram", **kwargs)
            return ScrapedContent(multimodal=sequence)

    async def _get_photo(self, url: str, **kwargs) -> ScrapedContent:
        """Retrieves content from an Instagram photo URL (can also be a reel)."""
        from scrapemm.integrations.decodo import decodo
        return await decodo.scrape(url, session=kwargs["session"], timeout=60,
                                   output_format=kwargs.get("output_format", "multimodal"))

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
