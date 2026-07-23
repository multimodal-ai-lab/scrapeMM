import asyncio
import logging
import re
from datetime import datetime
from urllib.parse import urlparse

import aiohttp
from ezmm import MultimodalSequence, Item
from ezmm.common.items import Video, Image
from tiktok_research_api import TikTokResearchAPI, QueryVideoRequest, QueryUserInfoRequest, Criteria, Query

from scrapemm.common.exceptions import ContentBlockedError, IPBannedError, TargetUnavailableError
from scrapemm.download import download_image
from scrapemm.common.retrieval_integration import RetrievalIntegration
from scrapemm.integrations.ytdlp import download_video_with_ytdlp
from scrapemm.secrets import get_secret

logger = logging.getLogger("scrapeMM")


class TikTok(RetrievalIntegration):
    """Integration for TikTok to retrieve videos and metadata.
    
    Works in two modes:
    1. API mode: Uses TikTok Research API for comprehensive metadata (requires credentials)
    2. Fallback mode: Uses yt-dlp (https://github.com/yt-dlp/yt-dlp?tab=readme-ov-file) for basic metadata and
        ideo download (no credentials needed, but may violate TikTok's Terms of Service)
    """

    name = "TikTok"
    domains = ["tiktok.com"]

    async def _connect(self):
        # Try to initialize TikTok Research API
        logging.getLogger("tiktok_research_api").setLevel(logging.WARNING)

        client_key = get_secret("tiktok_client_key")
        client_secret = get_secret("tiktok_client_secret")

        self.api_available = False
        self.api = None

        if client_key and client_secret:
            try:
                self.api = TikTokResearchAPI(
                    client_key=client_key,
                    client_secret=client_secret,
                    qps=5
                )
                self.api_available = True
                logger.info("✅ Successfully connected to TikTok Research API.")
            except ImportError:
                logger.info("⚠️ TikTok Research API package not installed. Using fallback mode.")
            except Exception as e:
                logger.info(f"⚠️ TikTok Research API connection failed: {e}. Using fallback mode.")

        mode = "API" if self.api_available else "yt-dlp only"
        logger.info(f"✅ TikTok integration ready ({mode} mode).")
        self.connected = True

    async def _get(self, url: str, **kwargs) -> MultimodalSequence:
        session = kwargs['session']
        max_video_size = kwargs.get('max_video_size')

        # Determine if this is a video or profile URL
        try:
            if self._is_video_url(url):
                return await self._get_video(url, session, max_video_size)
            else:
                return await self._get_user_profile(url, session)
        except Exception as e:
            if "Your IP address is blocked from accessing this post" in str(e):
                raise IPBannedError(f"TikTok prevents your IP address from accessing the post {url}")
            elif "This post may not be comfortable for some audiences" in str(e):
                raise ContentBlockedError("Video is blocked by TikTok for being 'uncomfortable for some audiences'.")
            else:
                raise e

    async def _get_video(self, url: str, session: aiohttp.ClientSession,
                         max_video_size=None) -> MultimodalSequence:
        """Retrieves video using TikTok Research API and yt-dlp."""
        video_id = self._extract_video_id(url)
        if not video_id:
            raise TargetUnavailableError("TikTok video not available.")

        try:
            # Create criteria to search for the specific video ID
            query_criteria = Criteria(
                operation="EQ",
                field_name="video_id",
                field_values=[video_id]
            )
            query = Query(and_criteria=[query_criteria])

            # Define the fields we want to retrieve
            video_fields = "id,create_time,username,region_code,video_description,video_duration,hashtag_names,view_count,like_count,comment_count,share_count,music_id,voice_to_text"

            # Create the video request
            video_request = QueryVideoRequest(
                fields=video_fields,
                query=query,
                max_count=1,
                start_date="20200101",
                end_date=datetime.now().strftime("%Y%m%d"),
            )

            # Execute the query asynchronously (API call is synchronous/blocking)
            videos, search_id, cursor, has_more, start_date, end_date = await asyncio.to_thread(
                self.api.query_videos,
                video_request,
                fetch_all_pages=False,
            )

            video_data = videos[0] if videos else None

            # Download the video using yt-dlp
            video, thumbnail, metadata = await download_video_with_ytdlp(url, session, max_video_size=max_video_size)

            return await self._create_video_sequence_from_api(video_data or metadata, video, thumbnail)

        except Exception as e:
            raise RuntimeError(f"Error retrieving TikTok video: {e}")

    async def _get_user_profile(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence:
        """Retrieves profile using TikTok Research API."""
        username = self._extract_username(url)
        if not username:
            raise TargetUnavailableError("TikTok user not available.")

        try:
            user_info_request = QueryUserInfoRequest(username=username)
            user_info = self.api.query_user_info(user_info_request)
        except Exception as e:
            raise RuntimeError(f"Error retrieving TikTok user profile with API: {e}")

        if not user_info:
            raise TargetUnavailableError(f"TikTok user @{username} not available.")

        return await self._create_profile_sequence_from_api(username, user_info, url, session)

    async def _create_video_sequence_from_api(self, metadata: dict, video: Video | None,
                                              thumbnail: Image | None) -> MultimodalSequence:
        """Creates MultimodalSequence from API data."""
        # Extract relevant metadata (coming from either TikTok Research API or yt-dlp)
        username = metadata.get('username') or metadata.get('uploader', 'Unknown')
        description = metadata.get('video_description') or metadata.get('description', '')
        create_time = metadata.get('create_time') or metadata.get('upload_date', 'Unknown')
        duration = metadata.get('video_duration') or metadata.get('duration', 0)
        view_count = metadata.get('view_count', 0)
        like_count = metadata.get('like_count', 0)
        comment_count = metadata.get('comment_count', 0)
        share_count = metadata.get('share_count', 0)
        hashtags = metadata.get('hashtag_names', [])
        voice_to_text = metadata.get('voice_to_text', '')
        region_code = metadata.get('region_code', 'Unknown')

        hashtags_text = f"Hashtags: {', '.join(['#' + tag for tag in hashtags])}" if hashtags else ""
        voice_text = f"Voice transcription: {voice_to_text}" if voice_to_text else ""

        text = f"""**TikTok Video** (API data)
Author: @{username}
Posted: {create_time}
Duration: {duration}s
Region: {region_code}
Views: {view_count:,} - Likes: {like_count:,} - Comments: {comment_count:,} - Shares: {share_count:,}
{hashtags_text}

{description}

{voice_text}"""

        items: list[Item | str] = [text]
        if video:
            items.append(video)
        elif thumbnail:
            items.append(thumbnail)

        return MultimodalSequence(items)

    async def _create_profile_sequence_from_api(self, username: str, user_info: dict, url: str,
                                                session: aiohttp.ClientSession) -> MultimodalSequence:
        """Creates MultimodalSequence from API profile data."""
        display_name = user_info.get('display_name', '')
        bio_description = user_info.get('bio_description', '')
        follower_count = user_info.get('follower_count', 0)
        following_count = user_info.get('following_count', 0)
        likes_count = user_info.get('likes_count', 0)
        video_count = user_info.get('video_count', 0)
        verified = user_info.get('is_verified', False)
        avatar_url = user_info.get('avatar_url', '')

        avatar = None
        if avatar_url:
            avatar = await download_image(avatar_url, session, ignore_small_images=False)

        text = f"""**TikTok Profile**
User: {display_name} (@{username})
{"Verified" if verified else "Not verified"}
Profile image: {avatar.reference if avatar else 'None'}

URL: {url}
Bio: {bio_description}

Metrics:
- Followers: {follower_count:,}
- Following: {following_count:,}
- Likes: {likes_count:,}
- Videos: {video_count:,}"""

        return MultimodalSequence(text)

    def _is_video_url(self, url: str) -> bool:
        """Determines if the URL is a TikTok video URL."""
        return '/video/' in url or 'vm.tiktok.com' in url or re.search(r'/\d{10,}', url)

    def _extract_video_id(self, url: str) -> str | None:
        """Extracts the video ID from a TikTok URL."""
        try:
            if 'vm.tiktok.com' in url:
                parsed = urlparse(url)
                path_parts = parsed.path.strip('/').split('/')
                if path_parts and path_parts[0]:
                    return path_parts[0]
            else:
                match = re.search(r'/video/(\d+)', url)
                if match:
                    return match.group(1)

                parsed = urlparse(url)
                path_parts = parsed.path.strip('/').split('/')
                for part in reversed(path_parts):
                    if part.isdigit() and len(part) >= 10:
                        return part

            return None
        except Exception as e:
            logger.error(f"❌ Error extracting video ID from {url}: {e}")
            return None

    def _extract_username(self, url: str) -> str | None:
        """Extracts the username from a TikTok profile URL."""
        try:
            match = re.search(r'/@([^/?]+)', url)
            if match:
                return match.group(1)

            parsed = urlparse(url)
            path_parts = parsed.path.strip('/').split('/')
            for part in path_parts:
                if part and not part.startswith('video') and not part.isdigit():
                    username = part.lstrip('@')
                    if username:
                        return username

            return None
        except Exception as e:
            logger.error(f"❌ Error extracting username from {url}: {e}")
            return None
