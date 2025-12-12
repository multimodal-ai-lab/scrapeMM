import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urlparse, parse_qs

import aiohttp
from ezmm import MultimodalSequence
from tweepy import Tweet, User, TooManyRequests
from tweepy.asynchronous import AsyncClient

import scrapemm.common
from scrapemm.common.exceptions import RateLimitError
from scrapemm.download import download_image, download_video
from scrapemm.integrations.base import RetrievalIntegration
from scrapemm.secrets import get_secret

logger = logging.getLogger("scrapeMM")


class X(RetrievalIntegration):
    """The X (Twitter) integration. Requires "Basic" API access to work. For more info, see
    https://developer.x.com/en/docs/twitter-api/getting-started/about-twitter-api#v2-access-level
    "Free" API access does NOT include reading Tweets."""
    name = "X (Twitter)"
    domains = ["twitter.com", "x.com", "t.co"]

    account_explanation = """X accounts having a "blue" verification fulfill a basic set of criteria,
    such as having a confirmed phone number. At time of the verification, the account must be not
    deceptive.
    
    An account with "gold" verification belongs to an "official organization" verified through X,
    costing about $1000 per month.
    
    If an account is "protected", it means that it was set private by the user.
    
    A "withheld" account is a user who got restricted by X.
    
    A "parody" account is an explicit, user-provided indication of being a parody (of someone or something).
    
    The "location" of a user profile is a user-provided string and is not guaranteed to be accurate."""

    async def _connect(self):
        bearer_token = get_secret("x_bearer_token")
        if bearer_token:
            self.client = AsyncClient(bearer_token=bearer_token, wait_on_rate_limit=scrapemm.common.WAIT_ON_RATE_LIMIT)
            self.connected = True
            logger.info("✅ Successfully connected to X.")
        else:
            self.connected = False
            logger.warning("❌ X (Twitter) integration not configured: Missing bearer token.")

    async def _get(self, url: str, **kwargs) -> Optional[MultimodalSequence]:
        session = kwargs.get("session")
        max_video_size = kwargs.get("max_video_size")
        url = self._normalize(url)
        tweet_id = extract_tweet_id_from_url(url)
        try:
            if tweet_id:
                return await self._get_tweet(tweet_id, session, max_video_size)
            else:
                username = extract_username_from_url(url)
                if username:
                    return await self._get_user(username, session)
        except TooManyRequests:
            raise RateLimitError("X API rate limit reached.")

    def _normalize(self, url: str) -> str:
        """Turns URLs of the form https://publish.twitter.com/?query=...
        into the bare Twitter URL."""
        if url.startswith("https://publish.twitter.com/?query="):
            query = urlparse(url).query
            return parse_qs(query).get("query", [])[0] or url
        return url

    async def _get_tweet(self, tweet_id: int, session: aiohttp.ClientSession, max_video_size: int = None) -> Optional[MultimodalSequence]:
        """Returns a MultimodalSequence containing the tweet's text and media
        along with information like metrics, etc."""

        response = await self.client.get_tweet(
            id=tweet_id,
            expansions=["author_id", "attachments.media_keys", "geo.place_id",
                        "edit_history_tweet_ids"],
            media_fields=["url", "variants"],
            tweet_fields=["created_at", "public_metrics"],
        )
        tweet: Tweet = response.data

        if tweet:
            media_raw = response.includes.get("media")
            author = response.includes.get("users")[0]
            metrics = tweet.public_metrics

            # Post-process text
            text = tweet.text
            text = re.sub(r"https?://t\.co/\S+", "", text).strip()

            # Download the media
            media = []
            if media_raw:
                for medium_raw in media_raw:
                    if medium_raw.type == "photo":
                        url = medium_raw.url
                        medium = await download_image(url, session=session)
                    elif medium_raw.type in ["video", "animated_gif"]:
                        # Get the variant with the highest bitrate
                        url = _get_best_quality_video_url(medium_raw.variants)
                        medium = await download_video(url, session=session)
                        if medium and medium.size > max_video_size:
                            logger.info(f"Removing video {medium.reference} because it exceeds the maximum size "
                                        f"of {max_video_size / 1024 / 1024:.2f} MB.")
                            medium = None
                    else:
                        raise ValueError(f"Unsupported media type: {medium_raw.type}")
                    if medium:
                        media.append(medium)

            tweet_str = f"""**Post on X**
Author: {author.name}, @{author.username}
Posted on: {tweet.created_at.strftime("%B %d, %Y at %H:%M")}
Likes: {metrics['like_count']} - Retweets: {metrics['retweet_count']} - Replies: {metrics['reply_count']} - Views: {metrics['impression_count']}

{text}"""  # TODO: Add edit history
            return MultimodalSequence([tweet_str, *media])

    async def _get_user(self, username: str, session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
        """Returns a MultimodalSequence containing the user's profile information
        incl. profile image and profile banner."""

        # The fields "parody" and "verified_followers_count" are fairly new. See
        # https://x.com/Safety/status/1877581125608153389
        # and https://x.com/XDevelopers/status/1865180409425715202
        response = await self.client.get_user(username=username, user_fields=[
            "created_at", "description", "location", "parody", "profile_banner_url", "profile_image_url",
            "protected", "public_metrics", "url", "verified", "verified_followers_count", "verified_type", "withheld"
        ])
        user: User = response.data

        if user:
            # Turn all the data into a multimodal sequence
            profile_image = profile_banner = None
            if profile_image_url := user.profile_image_url:
                profile_image_url = profile_image_url.replace("_normal", "")  # Use the original picture variant
                profile_image = await download_image(profile_image_url, session)
            if hasattr(user, "profile_banner_url"):
                profile_banner_url = user.profile_banner_url
                if profile_banner_url:
                    profile_banner = await download_image(profile_banner_url, session)

            verification_status_text = f"{'Verified' if user.verified else 'Not verified'}"
            if user.verified:
                verification_status_text += f" ({user.verified_type})"

            metrics = [f" - {k.capitalize().replace('_', ' ')}: {v}"
                       for k, v in user.public_metrics.items()]
            metrics_text = "\n".join(metrics)
            if hasattr(user, "verified_followers_count"):
                metrics_text += f"\n - Verified followers count: {user.verified_followers_count}"

            properties_text = f"- {verification_status_text}"
            if user.protected:
                properties_text += "\n- Protected"
            if user.withheld:
                properties_text += "\n- Withheld"
            if user.parody:
                properties_text += "\n- Marked as parody"

            text = f"""**Profile on X**
User: {user.name}, @{user.username}
Joined: {user.created_at.strftime("%B %d, %Y") if user.created_at else "Unknown"}
Profile image: {profile_image.reference if profile_image else 'None'}
Profile banner: {profile_banner.reference if profile_banner else 'None'}

URL: {user.url}
Location: {user.location}
Description: {user.description}

Metrics:
{metrics_text}

Account properties:
{properties_text}"""

            return MultimodalSequence(text)


def extract_username_from_url(url: str) -> Optional[str]:
    # TODO: Users may change their username, invalidating corresponding URLs. Handle this
    # by retrieving the author's ID of the linked tweet.
    parsed = urlparse(url)
    try:
        candidate = parsed.path.strip("/").split("/")[0]
        if candidate and len(candidate) >= 3:
            return candidate
    except IndexError:
        return None


def extract_tweet_id_from_url(url: str) -> Optional[int]:
    parsed = urlparse(url)
    id_candidate = parsed.path.strip("/").split("/")[-1]  # Takes variants (like short links) into account
    try:
        return int(id_candidate)
    except ValueError:
        return None


def _get_best_quality_video_url(variants: list) -> Optional[str]:
    """Returns the URL of the video variant that has the highest bitrate."""
    bitrate = -1
    best_url = None
    for variant in variants:
        if content_type := variant.get("content_type"):
            if content_type.startswith("video/") and variant["bit_rate"] > bitrate:
                bitrate = variant["bit_rate"]
                best_url = variant["url"]
    return best_url


if __name__ == "__main__":
    urls = [
        # "https://x.com/thinking_panda/status/1939348093155344491",  # Image
        # "https://x.com/PopBase/status/1938496291908030484",  # Multiple images
        # "https://x.com/AMAZlNGNATURE"  # Profile
        # "https://x.com/AMAZlNGNATURE/status/1917939518000210352",  # Video
        "https://x.com/GiFShitposting/status/1936904802082161085",  # GIF
    ]
    x = X()
    for url in urls:
        task = x.get(url)
        out = asyncio.run(task)
        print(out)
