import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp
from ezmm import MultimodalSequence

from scrapemm.common import RetrievalFailed
from scrapemm.common.exceptions import RateLimitError, TargetUnavailableError, AccessBlockedError
from scrapemm.server.download import download_image
from scrapemm.server.download.requests import _request_via_curl_cffi
from scrapemm.server.integrations.base import RetrievalIntegration
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.server.integrations.ytdlp import get_content_with_ytdlp
from scrapemm.server.secrets import get_secret
from scrapemm.server.util import to_scraped_content, parse_netscape_cookies
from scrapemm.server.paths import CONFIG_DIR

logger = logging.getLogger("scrapeMM")

# Post URLs come as /p/<code>/ and, more recently, with the author in front:
# /<username>/p/<code>/ (likewise for reels)
POST_REGEX = re.compile(
    r"instagram\.com/(?:[A-Za-z0-9._]+/)?(p|reels?|tv)/([A-Za-z0-9_-]+)")
EMBED_URL = "https://www.instagram.com/p/{shortcode}/embed/captioned/"

# The profile data Instagram's own web app loads. Answers anonymous requests only at
# times (else "Please wait a few minutes", require_login), a logged-in session reliably.
PROFILE_API_URL = "https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
INSTAGRAM_WEB_APP_ID = "936619743392459"  # Sent by instagram.com itself; required

# First path segments that are Instagram's own pages, not usernames
RESERVED_PATHS = {"explore", "accounts", "stories", "direct", "about", "legal", "developer",
                  "web", "api", "reels", "p", "reel", "tv", "challenge", "emails"}
# The embed page renders the post server-side. Instagram drops this marker on the
# post's medium, so its absence means the embed carries no post (deleted, private,
# or age-restricted).
EMBED_MEDIA_MARKER = "EmbeddedMediaImage"


class Instagram(RetrievalIntegration):
    name = "Instagram"
    domains = ["instagram.com"]
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
        """Retrieves content from an Instagram post, reel or profile URL."""
        if match := POST_REGEX.search(url):
            # The canonical form: neither yt-dlp nor the embed know the one that
            # carries the author in its path
            kind, shortcode = match.groups()
            kind = "reel" if kind.startswith("reel") else kind
            url = f"https://www.instagram.com/{kind}/{shortcode}/"

        # Determine if this is a video or profile URL
        if self._is_video_url(url):
            return await self._get_video(url, **kwargs)
        elif self._is_photo_url(url):
            # /p/ URLs can also be reels, so try both
            content = None
            verdict = None  # yt-dlp's finding that the post is gone or restricted
            try:
                content = await self._get_video(url, **kwargs)
            except (TargetUnavailableError, AccessBlockedError) as e:
                verdict = e
            except RetrievalFailed:
                pass  # E.g. a photo post, which has no video
            except RateLimitError:
                raise
            except Exception:
                # Needs closer investigation
                logger.debug(f"Instagram video retrieval failed for {url}", exc_info=True)

            if content and content.multimodal.has_videos():
                return content
            if verdict:
                # Public posts are on the embed page. If it lacks the post as well,
                # yt-dlp was right; rendering the post page would take a minute and
                # show the same.
                if content := await self._get_photo_from_embed(url, **kwargs):
                    return content
                if isinstance(verdict, TargetUnavailableError):
                    raise TargetUnavailableError(
                        "The Instagram post is not available: the link may be broken, "
                        "or the post was removed or is private.") from verdict
                raise verdict
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
        from scrapemm.server.integrations.decodo import decodo
        return await decodo.scrape(url, session=kwargs["session"], timeout=60,
                                   output_format=kwargs.get("output_format", "multimodal"))

    async def _get_photo_from_embed(self, url: str, **kwargs) -> ScrapedContent | None:
        """Reads the post off Instagram's embed page, which serves the post's medium
        and caption rendered server-side and without login. Returns None if that page
        does not carry the post, leaving it to the caller to fall back.

        Worth the try before anything else: the embed answers within a second, while
        rendering the post page itself through a proxy takes 30 to 50 seconds."""
        match = POST_REGEX.search(url)
        if not match:
            return None

        session = kwargs["session"]
        embed_url = EMBED_URL.format(shortcode=match.group(2))
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
        """Retrieves an Instagram profile: who it is, its figures, the profile picture
        and the latest posts. Read from Instagram's profile API where it answers, else
        from the profile page rendered through Decodo (Instagram serves the page
        itself as an empty shell)."""
        username = self._extract_username(url)
        if not username or username.lower() in RESERVED_PATHS:
            raise RetrievalFailed(f"{url} is neither an Instagram post nor a profile.")

        if user := await self._fetch_profile(username):
            return await self._profile_to_content(user, kwargs["session"])

        logger.debug(f"Instagram's profile API refused @{username}; rendering the page instead.")
        from scrapemm.server.integrations.decodo import decodo
        return await decodo.scrape(f"https://www.instagram.com/{username}/",
                                   session=kwargs["session"], timeout=60,
                                   output_format=kwargs.get("output_format", "multimodal"),
                                   max_video_size=kwargs.get("max_video_size"))

    async def _fetch_profile(self, username: str) -> dict | None:
        """The profile's data from Instagram's API, sent with the stored session if
        there is one. None if Instagram refuses to answer."""
        headers = {"x-ig-app-id": INSTAGRAM_WEB_APP_ID, "Accept": "*/*"}
        if get_secret("instagram_cookie"):
            cookies = parse_netscape_cookies(self.cookie_file)
            headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        # Instagram refuses aiohttp's TLS fingerprint outright
        result = await _request_via_curl_cffi(PROFILE_API_URL.format(username=username), headers,
                                              lookup=True)
        if result is None:
            return None
        status, _, body = result
        if status == 404:
            raise TargetUnavailableError(f"The Instagram profile @{username} does not exist.")
        if status != 200:
            logger.debug(f"Instagram's profile API answered {status} for @{username}: {body[:200]!r}")
            return None
        try:
            return json.loads(body)["data"]["user"] or None
        except (ValueError, KeyError, TypeError):
            logger.debug(f"Unexpected answer of Instagram's profile API for @{username}: {body[:200]!r}")
            return None

    @staticmethod
    async def _profile_to_content(user: dict, session) -> ScrapedContent:
        username = user.get("username", "")
        name = user.get("full_name") or username
        lines = [f"**Instagram Profile**",
                 f"{name} (@{username})" + (" ✓ verified" if user.get("is_verified") else "")]
        if category := user.get("category_name"):
            lines.append(f"Category: {category}")
        posts = user.get("edge_owner_to_timeline_media") or {}
        lines.append(f"{_count(user, 'edge_followed_by')} followers · "
                     f"{_count(user, 'edge_follow')} following · {posts.get('count', 0)} posts")
        if user.get("is_private"):
            lines.append("Private account: its posts are only visible to followers.")
        if link := user.get("external_url"):
            lines.append(f"Link: {link}")
        header = "\n".join(lines)
        if bio := user.get("biography"):
            header += f"\n\n{bio}"

        items: list = [header]
        if picture_url := user.get("profile_pic_url_hd") or user.get("profile_pic_url"):
            # Profile pictures are small; keep them anyway, they identify the account
            if picture := await download_image(picture_url, session, ignore_small_images=False):
                items.append(picture)

        recent = []
        for edge in posts.get("edges") or []:
            node = edge.get("node") or {}
            captions = (node.get("edge_media_to_caption") or {}).get("edges") or []
            caption = (captions[0].get("node", {}).get("text", "") if captions else "").strip()
            taken = node.get("taken_at_timestamp")
            date = f"{datetime.fromtimestamp(taken, tz=timezone.utc):%Y-%m-%d}" if taken else ""
            kind = "Video" if node.get("is_video") else "Post"
            line = f"- {date} {kind}: https://www.instagram.com/p/{node.get('shortcode')}/"
            if caption:
                line += f" — {caption[:200].replace(chr(10), ' ')}"
            recent.append(line)
        if recent:
            items.append("**Latest posts**\n" + "\n".join(recent))

        return ScrapedContent(multimodal=MultimodalSequence(items))

    def _is_video_url(self, url: str) -> bool:
        """Checks if the URL is an Instagram video URL."""
        return "instagram.com/reels" in url or "instagram.com/reel/" in url

    def _is_photo_url(self, url: str) -> bool:
        """Checks if the URL is an Instagram photo URL."""
        return "instagram.com/p/" in url or "instagram.com/tv/" in url

    def _extract_username(self, url: str) -> str:
        """Extracts the username from an Instagram profile URL."""
        parsed_url = urlparse(url)
        path_parts = parsed_url.path.strip('/').split('/')
        if len(path_parts) > 0:
            return path_parts[0].lstrip("@")
        return ""


def _count(user: dict, edge: str) -> str:
    """A follower-type count, as Instagram's API nests it."""
    count = (user.get(edge) or {}).get("count")
    return f"{count:,}" if isinstance(count, int) else "?"
