import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from ezmm import Item, MultimodalSequence, Video

from scrapemm.common.exceptions import (AccessBlockedError, RateLimitError,
                                        TargetUnavailableError)
from scrapemm.common.retrieval_integration import RetrievalIntegration
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.download import download_image, download_video
from scrapemm.download.util import looks_like_image_file_url
from scrapemm.secrets import get_secret
from scrapemm.util import unshorten

logger = logging.getLogger("scrapeMM")

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
INFO_URL = "https://oauth.reddit.com/api/info"

# Reddit requires API clients to identify themselves with a unique, descriptive
# User-Agent. See https://support.reddithelp.com/hc/en-us/articles/16160319875092
USER_AGENT = "python:scrapeMM:v0.8.0 (+https://github.com/multimodal-ai-lab/scrapeMM)"

# Reddit's CDN content-negotiates on the Accept header: with the browser-like default
# ("text/html,...") it answers media requests with an HTML page instead of the media.
MEDIA_HEADERS = {"Accept": "*/*"}

POST_ID_REGEX = re.compile(r"/comments/([a-z0-9]+)", re.IGNORECASE)
SHORTLINK_REGEX = re.compile(r"^https?://(?:www\.)?redd\.it/([a-z0-9]+)", re.IGNORECASE)
# Reddit's app share links, e.g. https://www.reddit.com/r/interestingasfuck/s/aBcDeF1234
SHARE_LINK_REGEX = re.compile(r"^/(?:r/[^/]+/)?s/[a-z0-9]+", re.IGNORECASE)


class Reddit(RetrievalIntegration):
    """Retrieves Reddit posts through Reddit's official API.

    Uses app-only ("client credentials") OAuth, so only a client ID and secret of a
    Reddit app are needed, no user account. Register an app of type "script" at
    https://www.reddit.com/prefs/apps to obtain them."""

    name = "Reddit"
    domains = ["reddit.com", "redd.it"]

    async def _connect(self):
        self.client_id = get_secret("reddit_client_id")
        self.client_secret = get_secret("reddit_client_secret")
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._token_lock = asyncio.Lock()

        if not (self.client_id and self.client_secret):
            logger.warning("❌ Reddit integration not configured: Missing client ID or client secret.")
            self.connected = False
            return

        logger.info("✅ Reddit integration ready.")
        self.connected = True

    async def _get(self, url: str, **kwargs) -> ScrapedContent:
        session = kwargs["session"]
        post_id = await self._extract_post_id(url, session)
        post = await self._fetch_post(post_id, session)
        media = await self._download_media(post, session, kwargs.get("max_video_size"))
        return ScrapedContent(multimodal=MultimodalSequence([self._describe(post), *media]))

    @staticmethod
    async def _extract_post_id(url: str, session: aiohttp.ClientSession) -> str:
        """Extracts the ID of the post the given URL points at."""
        if match := SHORTLINK_REGEX.match(url):
            return match.group(1)

        if SHARE_LINK_REGEX.match(urlparse(url).path):
            # Share links don't contain the post ID, so resolve the redirect first
            expanded = await unshorten(url, session)
            if not expanded:
                raise TargetUnavailableError(f"Could not resolve Reddit share link: {url}")
            url = expanded

        if match := POST_ID_REGEX.search(urlparse(url).path):
            return match.group(1)

        raise NotImplementedError(f"Only Reddit posts are supported, got: {url}")

    async def _access_token(self, session: aiohttp.ClientSession) -> str:
        """Returns a valid app-only access token, requesting a new one if needed."""
        async with self._token_lock:
            if self._token and time.time() < self._token_expiry:
                return self._token

            headers = {"Authorization": aiohttp.encode_basic_auth(self.client_id, self.client_secret),
                       "User-Agent": USER_AGENT}
            async with session.post(TOKEN_URL, headers=headers,
                                    data={"grant_type": "client_credentials"}) as response:
                if response.status in (401, 403):
                    raise AccessBlockedError("Reddit rejected the API credentials. Please check the "
                                             "'reddit_client_id' and 'reddit_client_secret' secrets.")
                if response.status == 429:
                    raise RateLimitError("Reddit API rate limit reached while requesting an access token.")
                response.raise_for_status()
                payload = await response.json()

            self._token = payload["access_token"]
            # Renew a minute early to never send an already expired token
            self._token_expiry = time.time() + payload.get("expires_in", 3600) - 60
            logger.info("✅ Successfully connected to Reddit.")
            return self._token

    async def _fetch_post(self, post_id: str, session: aiohttp.ClientSession) -> dict:
        """Fetches the post with the given ID from the Reddit API."""
        headers = {"Authorization": f"Bearer {await self._access_token(session)}",
                   "User-Agent": USER_AGENT}
        # raw_json=1 keeps the media URLs unescaped, otherwise they are not downloadable
        params = {"id": f"t3_{post_id}", "raw_json": "1"}

        async with session.get(INFO_URL, headers=headers, params=params) as response:
            if response.status == 429:
                raise RateLimitError("Reddit API rate limit reached.")
            if response.status in (403, 451):
                raise AccessBlockedError(f"Reddit denies access to post {post_id}.")
            if response.status == 404:
                raise TargetUnavailableError(f"Reddit post {post_id} does not exist.")
            response.raise_for_status()
            payload = await response.json()

        children = payload.get("data", {}).get("children") or []
        if not children:
            raise TargetUnavailableError(f"Reddit post {post_id} is not available (anymore).")
        return children[0]["data"]

    @staticmethod
    def _describe(post: dict) -> str:
        """Renders the post's text and metadata."""
        created = post.get("created_utc")
        posted_on = (datetime.fromtimestamp(created, tz=timezone.utc).strftime("%B %d, %Y at %H:%M UTC")
                     if created else "Unknown")
        upvote_ratio = post.get("upvote_ratio")

        lines = [
            "**Post on Reddit**",
            f"Subreddit: {post.get('subreddit_name_prefixed') or 'Unknown'}",
            f"Author: u/{post.get('author') or 'Unknown'}",
            f"Posted on: {posted_on}",
            f"Score: {post.get('score', 0)}"
            + (f" ({upvote_ratio:.0%} upvoted)" if upvote_ratio else "")
            + f" - Comments: {post.get('num_comments', 0)}",
        ]
        if flair := post.get("link_flair_text"):
            lines.append(f"Flair: {flair}")
        if post.get("over_18"):
            lines.append("Marked as NSFW")
        # Link posts point at some external page, which is worth keeping
        link = post.get("url_overridden_by_dest")
        if link and not post.get("is_gallery") and not post.get("is_video") \
                and not looks_like_image_file_url(link):
            lines.append(f"Links to: {link}")

        lines.append(f"\nTitle: {post.get('title') or ''}")
        if selftext := post.get("selftext"):
            lines.append(f"\n{selftext}")

        return "\n".join(lines)

    async def _download_media(self, post: dict, session: aiohttp.ClientSession,
                              max_video_size: int | None = None) -> list[Item]:
        """Downloads all media contained in the post."""
        if post.get("is_gallery"):
            return await self._download_gallery(post, session)

        media = post.get("secure_media") or post.get("media") or {}
        if reddit_video := media.get("reddit_video"):
            video = await self._download_reddit_video(reddit_video, session, max_video_size)
            return [video] if video else []

        # Single-image post
        url = post.get("url_overridden_by_dest") or ""
        if post.get("post_hint") == "image" or looks_like_image_file_url(url):
            # Reddit media is always deliberately posted content, so keep small images, too
            image = await download_image(url, session=session, headers=MEDIA_HEADERS,
                                         ignore_small_images=False)
            return [image] if image else []

        return []

    @staticmethod
    async def _download_gallery(post: dict, session: aiohttp.ClientSession) -> list[Item]:
        """Downloads the media of a gallery post, preserving the gallery's order."""
        metadata = post.get("media_metadata") or {}
        tasks = []

        for entry in (post.get("gallery_data") or {}).get("items") or []:
            medium = metadata.get(entry.get("media_id")) or {}
            if medium.get("status") != "valid":
                continue
            source = medium.get("s") or {}

            if medium.get("e") == "AnimatedImage":
                # Reddit serves GIFs as MP4 as well, which is far more compact
                if url := source.get("mp4"):
                    tasks.append(download_video(url, session=session, headers=MEDIA_HEADERS))
                elif url := source.get("gif"):
                    tasks.append(download_image(url, session=session, headers=MEDIA_HEADERS,
                                                ignore_small_images=False))
            elif url := source.get("u"):
                tasks.append(download_image(url, session=session, headers=MEDIA_HEADERS,
                                            ignore_small_images=False))

        return [medium for medium in await asyncio.gather(*tasks) if medium]

    @staticmethod
    async def _download_reddit_video(reddit_video: dict, session: aiohttp.ClientSession,
                                     max_video_size: int | None = None) -> Optional[Video]:
        """Downloads a video hosted by Reddit (v.redd.it)."""
        # Reddit keeps audio in a separate stream, so only the HLS playlist yields a video
        # with sound (requires FFmpeg). The fallback MP4 needs no FFmpeg but may be mute.
        for url in (reddit_video.get("hls_url"), reddit_video.get("fallback_url")):
            if not url:
                continue
            video = await download_video(url, session=session, headers=MEDIA_HEADERS)
            if video:
                if max_video_size and video.size > max_video_size:
                    logger.info(f"Removing video {video.reference} because it exceeds the maximum "
                                f"size of {max_video_size / 1024 / 1024:.2f} MB.")
                    return None
                return video

        return None
