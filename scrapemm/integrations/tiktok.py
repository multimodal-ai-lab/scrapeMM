import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import aiohttp
from ezmm import MultimodalSequence, download_image
from ezmm.common.items import Video, Image
from tiktok_research_api import TikTokResearchAPI, QueryVideoRequest, QueryUserInfoRequest, Criteria, Query

from scrapemm.integrations.base import RetrievalIntegration
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
    domains = ["tiktok.com", "vm.tiktok.com"]

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

    async def _get(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        # Determine if this is a video or profile URL
        if self._is_video_url(url):
            return await self._get_video(url, session)
        else:
            return await self._get_user_profile(url, session)

    async def _get_video(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves content from a TikTok video URL."""

        # Try API mode first if available
        if self.api_available:
            result = await self._get_video_with_api(url, session)
            if result:
                return result
            logger.warning("API method failed, falling back to yt-dlp...")

        # Fallback to yt-dlp mode
        else:
            return await self._get_video_with_ytdlp(url, session)

    async def _get_video_with_api(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves video using TikTok Research API."""
        video_id = self._extract_video_id(url)
        if not video_id:
            return None

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

            # Execute the query
            videos, search_id, cursor, has_more, start_date, end_date = self.api.query_videos(
                video_request,
                fetch_all_pages=False
            )

            if not videos or len(videos) == 0:
                return None

            video_data = videos[0]

            # Download the video using yt-dlp
            video = await self._download_video_with_ytdlp(url)

            return await self._create_video_sequence_from_api(video_data, url, video)

        except Exception as e:
            logger.error(f"❌ Error retrieving TikTok video with API: {e}")
            return None

    async def _get_video_with_ytdlp(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves video using only yt-dlp (no API required)."""
        try:
            # Get metadata and video using yt-dlp
            metadata = await self._extract_metadata_with_ytdlp(url)
            if not metadata:
                return None

            video = await self._download_video_with_ytdlp(url)
            thumbnail = await self._download_thumbnail_with_ytdlp(url, session)

            return await self._create_video_sequence_from_ytdlp(metadata, url, video, thumbnail)

        except Exception as e:
            logger.error(f"❌ Error retrieving TikTok video with yt-dlp: {e}")
            return None

    async def _get_user_profile(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves a TikTok user profile."""

        # Try API mode first if available
        if self.api_available:
            result = await self._get_profile_with_api(url, session)
            if result:
                return result
            logger.warning("API method failed for profile.")

        # For profiles, yt-dlp has very limited capabilities
        # We can only provide basic info that we can extract from the URL
        username = self._extract_username(url)
        if username:
            text = f"""**TikTok Profile**
Username: @{username}
URL: {url}

Note: Profile details require TikTok Research API access.
Configure API credentials for full profile information."""
            return MultimodalSequence([text])

        return None

    async def _get_profile_with_api(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence | None:
        """Retrieves profile using TikTok Research API."""
        username = self._extract_username(url)
        if not username:
            return None

        try:
            user_info_request = QueryUserInfoRequest(username=username)
            user_info = self.api.query_user_info(user_info_request)

            if not user_info:
                return None

            return await self._create_profile_sequence_from_api(user_info, url, session)

        except Exception as e:
            logger.error(f"❌ Error retrieving TikTok user profile with API: {e}")
            return None

    async def _extract_metadata_with_ytdlp(self, url: str) -> dict[str, Any] | None:
        """Extracts metadata using yt-dlp without downloading the video."""
        try:
            cmd = [
                sys.executable,  # Ensure to use the same Python interpreter as the calling process
                '-m',
                'yt_dlp',
                '--no-download',
                '--print-json',
                '--no-warnings',
                '--quiet',
                url
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                logger.error(f"❌ yt-dlp metadata extraction failed: {stderr.decode()}")
                return None

            # Parse JSON output
            metadata = json.loads(stdout.decode())
            return metadata

        except Exception as e:
            logger.error(f"❌ Error extracting metadata with yt-dlp: {e}")
            return None

    async def _download_video_with_ytdlp(self, url: str) -> Video | None:
        """Downloads a TikTok video using yt-dlp."""
        try:
            with tempfile.NamedTemporaryFile(suffix='.%(ext)s', delete=False) as temp_file:
                temp_path = temp_file.name

            cmd = [
                sys.executable,  # Ensure to use the same Python interpreter as the calling process
                '-m',
                'yt_dlp',
                '--no-playlist',
                '--no-warnings',
                '--quiet',
                '--format', 'best[ext=mp4]/best',
                '--output', temp_path,
                url
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                logger.warning(f"yt-dlp video download failed: {stderr.decode()}")
                return None

            # Find the actual downloaded file (yt-dlp changes extension)
            downloaded_file = None
            for ext in ['.mp4', '.webm', '.mkv']:
                potential_file = temp_path.replace('.%(ext)s', ext)
                if os.path.exists(potential_file) and os.path.getsize(potential_file) > 0:
                    downloaded_file = potential_file
                    break

            if downloaded_file and os.path.exists(downloaded_file):
                with open(downloaded_file, 'rb') as f:
                    video_data = f.read()

                os.unlink(downloaded_file)

                video = Video(binary_data=video_data, source_url=url)
                video.relocate(move_not_copy=True)
                return video

            return None

        except Exception as e:
            logger.error(f"❌ Error downloading video with yt-dlp: {e}")
            return None

    async def _download_thumbnail_with_ytdlp(self, url: str, session: aiohttp.ClientSession) -> Image | None:
        """Downloads thumbnail using yt-dlp metadata."""
        try:
            metadata = await self._extract_metadata_with_ytdlp(url)
            if not metadata:
                return None

            thumbnail_url = metadata.get('thumbnail')
            if thumbnail_url:
                return await download_image(thumbnail_url, session)

        except Exception as e:
            logger.error(f"❌ Error downloading thumbnail: {e}")

        return None

    async def _create_video_sequence_from_api(self, video_data: dict, url: str,
                                              video: Video | None) -> MultimodalSequence:
        """Creates MultimodalSequence from API data."""
        username = video_data.get('username', 'Unknown')
        description = video_data.get('video_description', '')
        create_time = video_data.get('create_time', 'Unknown')
        duration = video_data.get('video_duration', 0)
        view_count = video_data.get('view_count', 0)
        like_count = video_data.get('like_count', 0)
        comment_count = video_data.get('comment_count', 0)
        share_count = video_data.get('share_count', 0)
        hashtags = video_data.get('hashtag_names', [])
        voice_to_text = video_data.get('voice_to_text', '')
        region_code = video_data.get('region_code', 'Unknown')

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

        items = [text]
        if video:
            items.append(video)

        return MultimodalSequence(items)

    async def _create_video_sequence_from_ytdlp(self, metadata: dict, url: str, video: Video | None,
                                                thumbnail: Image | None) -> MultimodalSequence:
        """Creates MultimodalSequence from yt-dlp metadata."""
        # title = metadata.get('title', '')
        uploader = metadata.get('uploader', 'Unknown')
        upload_date = metadata.get('upload_date', '')
        duration = metadata.get('duration', 0)
        view_count = metadata.get('view_count', 0)
        like_count = metadata.get('like_count', 0)
        comment_count = metadata.get('comment_count', 0)
        description = metadata.get('description', '')

        # Format upload date
        formatted_date = upload_date
        if upload_date and len(upload_date) == 8:
            try:
                date_obj = datetime.strptime(upload_date, '%Y%m%d')
                formatted_date = date_obj.strftime('%Y-%m-%d')
            except ValueError:
                pass

        text = f"""**TikTok Video**
Author: @{uploader}
Posted: {formatted_date}
Duration: {duration}s
Views: {view_count:,} - Likes: {like_count:,} - Comments: {comment_count:,}

{description}"""

        items = [text]
        if thumbnail:
            items.append(thumbnail)
        if video:
            items.append(video)

        return MultimodalSequence(items)

    async def _create_profile_sequence_from_api(self, user_info: dict, url: str,
                                                session: aiohttp.ClientSession) -> MultimodalSequence:
        """Creates MultimodalSequence from API profile data."""
        username = user_info.get('username', 'Unknown')
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
            avatar = await download_image(avatar_url, session)

        text = f"""**TikTok Profile** (API data)
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

        items = [text]
        if avatar:
            items.append(avatar)

        return MultimodalSequence(items)

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
