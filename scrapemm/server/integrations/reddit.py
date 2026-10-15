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
from scrapemm.server.integrations.base import RetrievalIntegration
from scrapemm.common.scraping_response import ScrapedContent
from scrapemm.server.download import download_image, download_video
from scrapemm.server.download.util import looks_like_image_file_url, report_omitted
from scrapemm.server.secrets import get_secret
from scrapemm.server.util import unshorten

logger = logging.getLogger("scrapeMM")

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
# A post with its comments, in one request
COMMENTS_URL = "https://oauth.reddit.com/comments/{post_id}"

# Reddit requires API clients to identify themselves with a unique, descriptive
# User-Agent. See https://support.reddithelp.com/hc/en-us/articles/16160319875092
USER_AGENT = "python:scrapeMM:v0.8.0 (+https://github.com/multimodal-ai-lab/scrapeMM)"

# Reddit's CDN content-negotiates on the Accept header: with the browser-like default
# ("text/html,...") it answers media requests with an HTML page instead of the media.
MEDIA_HEADERS = {"Accept": "*/*"}

POST_ID_REGEX = re.compile(r"/comments/([a-z0-9]+)", re.IGNORECASE)
# A link to a comment: /comments/<post>/comment/<comment>/, or the older
# /comments/<post>/<title_slug>/<comment>/
COMMENT_ID_REGEX = re.compile(r"/comments/[a-z0-9]+/[^/]+/([a-z0-9]+)/?$", re.IGNORECASE)
SHORTLINK_REGEX = re.compile(r"^https?://(?:www\.)?redd\.it/([a-z0-9]+)", re.IGNORECASE)
# Reddit's app share links, e.g. https://www.reddit.com/r/interestingasfuck/s/aBcDeF1234
SHARE_LINK_REGEX = re.compile(r"^/(?:r/[^/]+/)?s/[a-z0-9]+", re.IGNORECASE)

# The comments taken along: the top comments (by score), each with its first replies.
# Reddit's `limit` counts replies too: 60 yields some 20 top comments, enough to skip the
# pinned ones and still have ten.
TOP_COMMENTS = 10
REPLIES_PER_COMMENT = 2
COMMENTS_LIMIT = 60
# For a link to a comment: its parents (at most 8, Reddit's maximum), the comment, and
# its replies as above. `depth` counts from the topmost parent.
COMMENT_CONTEXT = 8
COMMENT_DEPTH = COMMENT_CONTEXT + 3
COMMENT_LIMIT = 100

# Media in a comment's text: an uploaded image as a bare preview.redd.it/i.redd.it URL,
# a GIF as ![gif](giphy|<id>), an upload as ![img](<media id>), or a link to either
MEDIA_REF_REGEX = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)|(https://(?:preview|i)\.redd\.it/[^\s)\]]+)")
REDDIT_CDN_REGEX = re.compile(r"https://(?:preview|i)\.redd\.it/")


class Reddit(RetrievalIntegration):
    """Retrieves Reddit posts through Reddit's official API.

    Uses app-only ("client credentials") OAuth, so only a client ID and secret of a
    Reddit app are needed, no user account. Register an app of type "script" at
    https://www.reddit.com/prefs/apps to obtain them."""

    name = "Reddit"
    post_media = True
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
        post_id, comment_id = await self._extract_ids(url, session)
        post, comments = await self._fetch_thread(post_id, comment_id, session)
        media, discussion = await asyncio.gather(
            self._download_media(post, session, kwargs.get("max_video_size")),
            self._render_comments(post, comments, comment_id, session))
        return ScrapedContent(multimodal=MultimodalSequence(
            _joined([self._describe(post), *media, *discussion])))

    @staticmethod
    async def _extract_ids(url: str, session: aiohttp.ClientSession) -> tuple[str, Optional[str]]:
        """The ID of the post the given URL points at, and of the comment, if it points
        at one."""
        if match := SHORTLINK_REGEX.match(url):
            return match.group(1), None

        if SHARE_LINK_REGEX.match(urlparse(url).path):
            # Share links don't contain the post ID, so resolve the redirect first
            expanded = await unshorten(url, session)
            if not expanded:
                raise TargetUnavailableError(f"Could not resolve Reddit share link: {url}")
            url = expanded

        path = urlparse(url).path
        if match := POST_ID_REGEX.search(path):
            comment = COMMENT_ID_REGEX.search(path)
            return match.group(1), comment.group(1) if comment else None

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

    async def _fetch_thread(self, post_id: str, comment_id: Optional[str],
                            session: aiohttp.ClientSession) -> tuple[dict, list[dict]]:
        """Fetches the post with the given ID and its top comments from the Reddit API;
        with a `comment_id`, that comment's thread instead: its parents, the comment and
        its replies. Returns the post and the comment tree's top level (Reddit's "things":
        comments, "t1", and stubs for more of them, "more")."""
        headers = {"Authorization": f"Bearer {await self._access_token(session)}",
                   "User-Agent": USER_AGENT}
        # raw_json=1 keeps the media URLs unescaped, otherwise they are not downloadable
        params = {"raw_json": "1", "sort": "top", "depth": "2", "limit": str(COMMENTS_LIMIT)}
        if comment_id:
            params |= {"comment": comment_id, "context": str(COMMENT_CONTEXT),
                       "depth": str(COMMENT_DEPTH), "limit": str(COMMENT_LIMIT)}

        async with session.get(COMMENTS_URL.format(post_id=post_id), headers=headers,
                               params=params) as response:
            if response.status == 429:
                raise RateLimitError("Reddit API rate limit reached.")
            if response.status in (403, 451):
                raise AccessBlockedError(f"Reddit denies access to post {post_id}.")
            if response.status == 404:
                raise TargetUnavailableError(f"Reddit post {post_id} does not exist.")
            response.raise_for_status()
            payload = await response.json()

        post_listing, comment_listing = payload if isinstance(payload, list) and len(payload) == 2 \
            else ({}, {})
        children = post_listing.get("data", {}).get("children") or []
        if not children:
            raise TargetUnavailableError(f"Reddit post {post_id} is not available (anymore).")
        return children[0]["data"], comment_listing.get("data", {}).get("children") or []

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
            image = await download_image(url, session=session, headers=MEDIA_HEADERS)
            return [image] if image else []

        return []

    @staticmethod
    async def _download_gallery(post: dict, session: aiohttp.ClientSession) -> list[Item]:
        """Downloads the media of a gallery post, preserving the gallery's order."""
        metadata = post.get("media_metadata") or {}
        tasks = []

        for entry in (post.get("gallery_data") or {}).get("items") or []:
            medium = metadata.get(entry.get("media_id")) or {}
            if medium.get("status") == "valid":
                tasks.append(_download_listed(medium, session))

        return [medium for medium in await asyncio.gather(*tasks) if medium]

    async def _render_comments(self, post: dict, comments: list[dict], comment_id: Optional[str],
                               session: aiohttp.ClientSession) -> list[str | Item]:
        """The comments as text with their media in place: for a post, its top comments
        with their first replies; for a comment, its parents, the comment, and its
        replies likewise. Replies are quoted one level deeper than what they answer."""
        shown: list[tuple[dict, int, bool]] = []  # (comment, quote depth, whether linked)
        thread = _chain_to(comments, comment_id) if comment_id else None
        if thread:
            *parents, linked = thread
            shown += [(parent, depth, False) for depth, parent in enumerate(parents)]
            shown.append((linked, len(parents), True))
            shown += _with_replies(_comments_in(linked.get("replies"))[:TOP_COMMENTS],
                                   len(parents) + 1)
            heading = (f"**Comments** (the linked comment"
                       f"{f', below the {len(parents)} it replies to' if parents else ''},"
                       f" with its top replies)")
        else:
            top = [c for c in _comments_in(comments)
                   if not c.get("stickied") and c.get("author") != "AutoModerator"]
            shown += _with_replies(top[:TOP_COMMENTS], 0)
            if not shown:
                return []
            heading = (f"**Comments** (top {min(len(top), TOP_COMMENTS)} of "
                       f"{post.get('num_comments', 0):,} by score, with their top replies)")
            if comment_id:
                heading += f"\nThe linked comment ({comment_id}) is not available (anymore)."

        rendered = await asyncio.gather(*(_render_comment(comment, depth, linked, session)
                                          for comment, depth, linked in shown))
        return [f"\n\n{heading}", *(part for parts in rendered for part in parts)]

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
                    report_omitted(url, max_video_size, video.size)
                    return None
                return video

        return None


def _comments_in(things) -> list[dict]:
    """The comments among Reddit's "things" (a list, or a listing as in `replies`)."""
    if isinstance(things, dict):
        things = things.get("data", {}).get("children") or []
    return [thing["data"] for thing in things or [] if thing.get("kind") == "t1"]


def _with_replies(comments: list[dict], depth: int) -> list[tuple[dict, int, bool]]:
    """The comments at quote `depth`, each followed by its first replies."""
    shown = []
    for comment in comments:
        shown.append((comment, depth, False))
        shown += [(reply, depth + 1, False)
                  for reply in _comments_in(comment.get("replies"))[:REPLIES_PER_COMMENT]]
    return shown


def _chain_to(things, comment_id: str) -> Optional[list[dict]]:
    """The comments from the top of the tree down to the one with `comment_id`, which
    comes last; None if it is not in the tree."""
    for comment in _comments_in(things):
        if comment.get("id") == comment_id:
            return [comment]
        if below := _chain_to(comment.get("replies"), comment_id):
            return [comment, *below]
    return None


async def _render_comment(comment: dict, depth: int, linked: bool,
                          session: aiohttp.ClientSession) -> list[str | Item]:
    """One comment: who wrote it when, its score, and its text with its media in place,
    each line quoted `depth` levels deep."""
    quote = "> " * depth
    created = comment.get("created_utc")
    date = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") \
        if created is not None else "unknown date"
    score = comment.get("score", 0)
    header = (f"**u/{comment.get('author') or '[deleted]'}** · {score:,} point{'' if score == 1 else 's'}"
              f" · {date}{' · **the linked comment**' if linked else ''}")
    body = (comment.get("body") or "").splitlines()

    # The media, downloaded all at once, then put where the text references them
    metadata = comment.get("media_metadata") or {}
    matches = [list(MEDIA_REF_REGEX.finditer(line)) for line in body]
    media = iter(await asyncio.gather(*(_download_ref(m.group(1) or m.group(2), metadata, session)
                                        for line in matches for m in line)))

    # Set off by an empty line, inside the quote of what it replies to
    parts: list[str | Item] = [f"{quote.rstrip()}\n" if depth else "\n", f"{quote}{header}\n"]
    for line, found in zip(body, matches):
        parts.append(quote if line.strip() else f"{quote.rstrip()}")
        position = 0
        for match in found:
            if (medium := next(media)) is None:
                continue  # Not a medium (a plain link), or not downloadable: left as text
            parts += [line[position:match.start()], medium]
            position = match.end()
        parts.append(line[position:] + "\n")
    return parts


async def _download_ref(ref: str, metadata: dict, session: aiohttp.ClientSession) -> Optional[Item]:
    """The medium a comment references: by its ID in the comment's `media_metadata`, as
    a GIF from Giphy, or by the URL of an image on Reddit's CDN. None for anything else."""
    is_cdn = bool(REDDIT_CDN_REGEX.match(ref))
    # An uploaded image's URL is named after its ID: preview.redd.it/<id>.jpeg?...
    medium_id = ref.split("?")[0].rsplit("/", 1)[-1].rsplit(".", 1)[0] if is_cdn else ref
    if (medium := metadata.get(medium_id)) and medium.get("s"):
        if found := await _download_listed(medium, session):
            return found
    if ref.startswith("giphy|"):
        giphy_id = ref.split("|", 1)[1].split("|")[0]
        return await download_image(f"https://media.giphy.com/media/{giphy_id}/giphy.gif",
                                    session=session)
    if is_cdn:
        return await download_image(ref, session=session, headers=MEDIA_HEADERS)
    return None


async def _download_listed(medium: dict, session: aiohttp.ClientSession) -> Optional[Item]:
    """Downloads a medium as Reddit lists it in `media_metadata` (galleries, comments)."""
    source = medium.get("s") or {}
    if medium.get("e") == "AnimatedImage":
        # Reddit serves GIFs as MP4 as well, which is far more compact
        if url := source.get("mp4"):
            return await download_video(url, session=session, headers=MEDIA_HEADERS)
        if url := source.get("gif"):
            return await download_image(url, session=session, headers=MEDIA_HEADERS)
    elif url := source.get("u"):
        return await download_image(url, session=session, headers=MEDIA_HEADERS)
    return None


def _joined(parts: list) -> list:
    """The parts with adjacent texts joined: ezMM would put a space between them."""
    joined: list = []
    for part in parts:
        if isinstance(part, str) and joined and isinstance(joined[-1], str):
            joined[-1] += part
        elif part != "":
            joined.append(part)
    return joined
