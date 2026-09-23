import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import aiohttp
from ezmm import MultimodalSequence, Item
from ezmm.common.items import Video, Image
from tiktok_research_api import (TikTokResearchAPI, QueryUserInfoRequest, Criteria, Query,
                                 APIErrorResponse)

from scrapemm.common.exceptions import AccessBlockedError, TargetUnavailableError
from scrapemm.server.download import download_image
from scrapemm.server.integrations.base import RetrievalIntegration
from scrapemm.server.integrations import decodo
from scrapemm.server.integrations.ytdlp import download_video_with_ytdlp
from scrapemm.server.secrets import get_secret
from scrapemm.common.scraping_response import ScrapedContent, OutputFormat
from scrapemm.server.util import to_scraped_content, preprocess_html

logger = logging.getLogger("scrapeMM")

# Earliest plausible TikTok upload date, used to sanity-check timestamps decoded
# from video IDs.
TIKTOK_LAUNCH = datetime(2016, 1, 1, tzinfo=timezone.utc)

# Metadata fields requested from the Research API's video query endpoint.
VIDEO_FIELDS = ("id,create_time,username,region_code,video_description,video_duration,"
                "hashtag_names,view_count,like_count,comment_count,share_count,music_id,"
                "voice_to_text")

# Error codes worth a second attempt. Everything else (a malformed query, an exhausted
# quota) fails the same way no matter how often we ask.
RETRYABLE_ERROR_CODES = (APIErrorResponse.ACCESS_TOKEN_INVALID, APIErrorResponse.TIMEOUT)


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
        self.api_semaphore = asyncio.Semaphore(5)

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

    async def _get(self, url: str, **kwargs) -> ScrapedContent:
        session = kwargs['session']
        max_video_size = kwargs.get('max_video_size')

        # Determine if this is a video, photo, or profile URL
        try:
            if self._is_video_url(url):
                return await self._get_video(url, session, max_video_size)
            elif self._is_photo_url(url):
                return await self._get_photo(url, session, kwargs.get('output_format', 'multimodal'))
            else:
                return await self._get_user_profile(url, session)
        except Exception as e:
            if "Your IP address is blocked from accessing this post" in str(e):
                raise AccessBlockedError(f"TikTok prevents your IP address from accessing the post {url}")
            elif "This post may not be comfortable for some audiences" in str(e):
                raise AccessBlockedError("Video is blocked by TikTok for being 'uncomfortable for some audiences'.")
            else:
                raise e

    async def _get_video(self, url: str, session: aiohttp.ClientSession,
                         max_video_size=None) -> ScrapedContent:
        """Retrieves video using TikTok Research API and yt-dlp."""
        video_id = self._extract_video_id(url)
        if not video_id:
            raise TargetUnavailableError("TikTok video not available.")

        try:
            # The API metadata only enriches what yt-dlp returns anyway, so fetch both
            # at once instead of making the download wait for the query.
            video_data, (video, thumbnail, metadata) = await asyncio.gather(
                self._query_video_metadata(video_id, session),
                download_video_with_ytdlp(url, session, max_video_size=max_video_size),
            )

            sequence = await self._create_video_sequence_from_api(video_data or metadata, video, thumbnail)
            return ScrapedContent(multimodal=sequence)

        except Exception as e:
            raise RuntimeError(f"Error retrieving TikTok video: {e}")

    async def _query_video_metadata(self, video_id: str,
                                    session: aiohttp.ClientSession) -> dict | None:
        """Looks up a single video's metadata in the TikTok Research API.

        Returns None if the API is unavailable, the query fails, or the video is not
        indexed -- the caller then falls back to the yt-dlp metadata.

        The endpoint requires a date range and the client library splits that range
        into 30-day chunks, firing one blocking request per chunk and continuing even
        after the video was found. Querying the full TikTok era therefore cost ~80
        requests and over three minutes per video. Since TikTok video IDs are
        Snowflake-like, we decode the upload time from the ID and query just that day.

        We post to the endpoint ourselves rather than going through the client library,
        which sleeps a second and retries up to 60 times on *any* error -- turning a
        malformed query or an exhausted quota into a minute-long stall -- and which does
        its waiting with a blocking `time.sleep` on shared, unsynchronized state.
        """
        if not self.api_available:
            return None

        created_at = self._created_at(video_id)
        if created_at is None:
            logger.debug(f"Could not decode the upload date of TikTok video {video_id}; "
                         f"skipping the Research API query and using yt-dlp metadata only.")
            return None

        query = Query(and_criteria=[Criteria(
            operation="EQ",
            field_name="video_id",
            field_values=[video_id],
        )])
        body = {
            "query": query.to_dict(),
            # One day of slack on either side absorbs whatever timezone the API applies.
            "start_date": (created_at - timedelta(days=1)).strftime("%Y%m%d"),
            "end_date": (created_at + timedelta(days=1)).strftime("%Y%m%d"),
            "max_count": 1,
        }
        endpoint = f"{self.api.url}/v2/research/video/query/?fields={VIDEO_FIELDS}"

        error = {}
        # Cap how many of these queries run at once: bypassing the client library also
        # bypasses its (blocking) rate limiter, and a batch retrieval runs up to 40 URLs
        # in parallel.
        async with self.api_semaphore:
            for attempt in range(2):
                try:
                    async with session.post(endpoint, json=body, headers=self.api.headers()) as response:
                        payload = await response.json()
                except Exception as e:
                    logger.warning(f"TikTok Research API query for video {video_id} failed: {e}")
                    return None

                error = payload.get("error") or {}
                if error.get("code") == APIErrorResponse.OK:
                    videos = (payload.get("data") or {}).get("videos") or []
                    return videos[0] if videos else None

                if error.get("code") not in RETRYABLE_ERROR_CODES or attempt:
                    break

                if error.get("code") == APIErrorResponse.ACCESS_TOKEN_INVALID:
                    # The client fetches its access token once on connect and never renews it.
                    await asyncio.to_thread(self.api.refresh_token)

        logger.warning(f"TikTok Research API query for video {video_id} failed: "
                       f"{error.get('code')}: {error.get('message')}")
        return None

    @staticmethod
    def _created_at(video_id: str) -> datetime | None:
        """Decodes the upload time from a TikTok video ID, or None if it does not fit.

        TikTok IDs are Snowflake-like: the upper 32 bits hold the Unix timestamp. Short
        links (vm.tiktok.com) carry an opaque slug instead, hence the plausibility check.
        """
        if not video_id.isdigit():
            return None
        try:
            created_at = datetime.fromtimestamp(int(video_id) >> 32, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        if not TIKTOK_LAUNCH <= created_at <= datetime.now(timezone.utc) + timedelta(days=1):
            return None
        return created_at

    async def _get_photo(self, url: str, session: aiohttp.ClientSession,
                         output_format: OutputFormat = "multimodal") -> ScrapedContent:
        content = await decodo.scrape(url, session, output_format="html")
        html = self._prepare_photo_html(content.html)
        return await to_scraped_content(html, session=session, output_format=output_format, url=url)

    @staticmethod
    def _prepare_photo_html(html: str) -> str:
        """Strip carousel clone slides and redundant CDN variants from Decodo HTML.

        TikTok photo posts use Swiper, which duplicates slides for infinite scroll
        (same img src repeated 3×). When both preview and origin CDN URLs exist,
        keep only the full-size origin variant.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(preprocess_html(html), "html.parser")

        for dup in soup.select(".swiper-slide-duplicate"):
            dup.decompose()

        page_text = str(soup)
        if "tiktokx-origin" in page_text:
            for img in soup.find_all("img"):
                src = img.get("src") or ""
                if "photomode" in src:
                    img.decompose()

        for img in soup.find_all("img"):
            src = img.get("src") or ""
            if "/100x100/" in src or "/avatar" in src.lower():
                img.decompose()

        return str(soup)

    async def _get_user_profile(self, url: str, session: aiohttp.ClientSession) -> ScrapedContent:
        """Retrieves profile using TikTok Research API."""
        username = self._extract_username(url)
        if not username:
            raise TargetUnavailableError("TikTok user not available.")

        if not self.api_available:
            raise RuntimeError("Retrieving TikTok profiles requires TikTok Research API credentials.")

        try:
            user_info_request = QueryUserInfoRequest(username=username)
            # The API call is synchronous/blocking, so keep it off the event loop --
            # otherwise it stalls every other URL being retrieved in parallel.
            user_info = await asyncio.to_thread(self.api.query_user_info, user_info_request)
        except Exception as e:
            raise RuntimeError(f"Error retrieving TikTok user profile with API: {e}")

        if not user_info:
            raise TargetUnavailableError(f"TikTok user @{username} not available.")

        sequence = await self._create_profile_sequence_from_api(username, user_info, url, session)
        return ScrapedContent(multimodal=sequence)

    async def _create_video_sequence_from_api(self, metadata: dict, video: Video | None,
                                              thumbnail: Image | None) -> MultimodalSequence:
        """Creates MultimodalSequence from API data."""
        # Extract relevant metadata (coming from either TikTok Research API or yt-dlp)
        username = metadata.get('username') or metadata.get('uploader', 'Unknown')
        description = metadata.get('video_description') or metadata.get('description', '')
        create_time = self._format_post_date(metadata)
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

    @staticmethod
    def _format_post_date(metadata: dict) -> str:
        """Renders the upload date as YYYY-MM-DD.

        The Research API reports `create_time` as a Unix timestamp, yt-dlp reports
        `upload_date` as a YYYYMMDD string.
        """
        if create_time := metadata.get('create_time'):
            try:
                return datetime.fromtimestamp(int(create_time), timezone.utc).strftime('%Y-%m-%d')
            except (TypeError, ValueError, OverflowError, OSError):
                logger.debug(f"Could not parse TikTok create_time: {create_time!r}")

        if upload_date := metadata.get('upload_date'):
            try:
                return datetime.strptime(str(upload_date), '%Y%m%d').strftime('%Y-%m-%d')
            except ValueError:
                logger.debug(f"Could not parse yt-dlp upload_date: {upload_date!r}")
                return str(upload_date)

        return 'Unknown'

    def _is_video_url(self, url: str) -> bool:
        """Determines if the URL is a TikTok video URL."""
        return '/video/' in url or 'vm.tiktok.com' in url

    def _is_photo_url(self, url: str) -> bool:
        """Determines if the URL is a TikTok photo URL."""
        return '/photo/' in url

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
