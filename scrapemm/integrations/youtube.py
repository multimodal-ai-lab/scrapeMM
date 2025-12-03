import logging
from typing import Optional

from ezmm import MultimodalSequence

from scrapemm.integrations.ytdlp import get_content_with_ytdlp
from .base import RetrievalIntegration
from scrapemm.secrets import get_secret
from ..common import CONFIG_DIR

logger = logging.getLogger("scrapeMM")


class YouTube(RetrievalIntegration):
    """YouTube integration for downloading videos and shorts using yt-dlp.
    YouTube is rate-limited to 333 videos per hour."""

    name = "YouTube"
    domains = [
        "youtube.com",
        "youtu.be",
    ]
    cookie_file = CONFIG_DIR / "youtube_cookie.txt"

    async def _connect(self):
        self.connected = True  # Connect always by default

        cookie = get_secret("youtube_cookie")
        if cookie:
            # Save the cookie in a .txt file next to the secrets file
            with open(self.cookie_file, "w") as f:
                f.write(cookie)
            logger.info(f"✅ Using cookie to connect to YouTube.")
        else:
            logger.warning(f"⚠️ Missing YouTube cookie. Won't be able to download videos, only thumbnails and metadata.")

    async def _get(self, url: str, **kwargs) -> Optional[MultimodalSequence]:
        """Downloads YouTube video or short using yt-dlp."""
        return await get_content_with_ytdlp(url,
                                            platform="YouTube",
                                            cookie_file=self.cookie_file.as_posix(),
                                            **kwargs)
