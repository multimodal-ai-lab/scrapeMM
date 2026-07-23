import logging

import aiohttp
from atproto_client.exceptions import RequestErrorBase
from ezmm import MultimodalSequence

from scrapemm.common.exceptions import TargetUnavailableError
from scrapemm.common.retrieval_integration import RetrievalIntegration
from scrapemm.download import download_video, download_image
from scrapemm.secrets import get_secret

logger = logging.getLogger("scrapeMM")


class Bluesky(RetrievalIntegration):
    name = "Bluesky"
    domains = ["bsky.app"]

    async def _connect(self):
        self.username = get_secret("bluesky_username")
        self.password = get_secret("bluesky_password")

        if not (self.username and self.password):
            logger.warning("❌ Bluesky integration not configured: Missing username or password.")
            self.connected = False
            return

        from atproto import AsyncClient
        self.client = AsyncClient()
        await self._authenticate()

    async def _get(self, url: str, **kwargs) -> MultimodalSequence:
        session = kwargs["session"]
        max_video_size = kwargs.get("max_video_size")
        if "post" in url:
            return await self._retrieve_post(url, session, max_video_size)
        else:
            return await self._retrieve_profile(url, session)

    async def _retrieve_post(
            self,
            url: str,
            session: aiohttp.ClientSession,
            max_video_size: int | None = None
    ) -> MultimodalSequence:
        """Retrieve a post from the given Bluesky URL."""
        uri = await self._construct_uri(url)
        if not uri:
            raise RuntimeError(f"Could not construct URI for Bluesky post: {url}")

        try:
            thread_response = await self.client.get_post_thread(uri=uri, depth=0, parent_height=0)
            thread = thread_response.thread

            if hasattr(thread, 'py_type'):
                thread_type = getattr(thread, 'py_type')
                if thread_type == 'app.bsky.feed.defs#notFoundPost':
                    raise Exception(f"Post not found for url {url}")
                if thread_type == 'app.bsky.feed.defs#blockedPost':
                    raise Exception(f"Post is blocked for url {url}")

            # Extract post data
            post_view = thread.post
            record = post_view.record

            # Basic post information
            post_text = record.text if hasattr(record, 'text') else ''
            created_at_str = record.created_at[:-1] if hasattr(record, 'created_at') else None

            # Author information
            author = post_view.author
            author_username = author.handle if hasattr(author, 'handle') else ''
            author_display_name = author.display_name if hasattr(author, 'display_name') else ''

            # Engagement metrics
            like_count = post_view.like_count if hasattr(post_view, 'like_count') else 0
            comment_count = post_view.reply_count if hasattr(post_view, 'reply_count') else 0
            share_count = post_view.repost_count if hasattr(post_view, 'repost_count') else 0

            # Extract media (images)
            media = []
            # Check for embedded images in the post
            if hasattr(post_view, 'embed'):
                embed = post_view.embed

                # For image embeds
                if hasattr(embed, 'py_type') and getattr(embed, 'py_type') == 'app.bsky.embed.images#view':
                    for img in embed.images:
                        if hasattr(img, 'fullsize'):
                            img_url = img.fullsize
                            img = await download_image(img_url, session)
                            media.append(img)
                # For video embeds
                elif hasattr(embed, 'py_type') and getattr(embed, 'py_type') == 'app.bsky.embed.video#view':
                    video = await download_video(embed.playlist, session)
                    if video:
                        if max_video_size is None or video.size <= max_video_size:
                            media.append(video)
                        else:
                            logger.info(f"Removing video {video.reference} because it exceeds the maximum size "
                                        f"of {max_video_size / 1024 / 1024:.2f} MB.")

                        # Extract hashtags and mentions
            hashtags, mentions, external_links = [], [], []
            # Parse facets (rich text features like links, mentions, etc.)
            if hasattr(record, 'facets') and record.facets:
                for facet in record.facets:
                    if hasattr(facet, 'features'):
                        for feature in facet.features:
                            if hasattr(feature, 'py_type'):
                                feature_type = getattr(feature, 'py_type')
                                if feature_type == 'app.bsky.richtext.facet#tag':
                                    hashtags.append(feature.tag if hasattr(feature, 'tag') else '')
                                elif feature_type == 'app.bsky.richtext.facet#mention':
                                    mentions.append(feature.did if hasattr(feature, 'did') else '')
                                elif feature_type == 'app.bsky.richtext.facet#link':
                                    external_links.append(feature.uri)

            # Check if this is a reply
            is_reply, reply_to = False, None
            if hasattr(record, 'reply'):
                is_reply = True
                # Get the parent post's author
                if hasattr(record.reply, 'parent') and hasattr(record.reply.parent, 'uri'):
                    parent_uri = record.reply.parent.uri
                    post_id = parent_uri.split('/')[-1]
                    reply_to_post = (await self.client.get_posts([parent_uri])).posts[0]
                    reply_to_author = reply_to_post.author
                    reply_to = f"https://bsky.app/profile/{reply_to_author.handle}/post/{post_id}"

            text = f"""**Post on Bluesky**
Author: {author_display_name} (@{author_username})
Posted on: {created_at_str}
Likes: {like_count} - Comments: {comment_count} - Shares: {share_count}
{"Reply to: " + reply_to if is_reply and reply_to else ""}
{post_text}"""
            return MultimodalSequence([text, *media])

        except RequestErrorBase as e:
            response = e.response
            code = response.status_code
            if code in [400, 404]:
                raise TargetUnavailableError("Post is unavailable.")
            else:
                raise

    async def _retrieve_profile(self, url: str, session: aiohttp.ClientSession) -> MultimodalSequence:
        """Retrieve a profile from the given Bluesky URL."""
        profile = await self.client.get_profile(url.split('/')[-1])

        avatar = await download_image(profile.avatar, session) if profile.avatar else None
        banner = await download_image(profile.banner, session) if profile.banner else None

        text = f"""**Profile on Bluesky**
User: {profile.display_name} (@{profile.handle})
Created on: {profile.created_at}
Profile image: {avatar.reference if avatar else 'None'}
Profile banner: {banner.reference if banner else 'None'}

URL: {url}
Description: {profile.description or 'No description provided'}

Metrics:
- Follower count: {profile.followers_count}
- Following count: {profile.follows_count}
- Post count: {profile.posts_count}
            """
        return MultimodalSequence(text)

    async def _authenticate(self) -> bool:
        """Authenticate with Bluesky using provided credentials."""
        try:
            await self.client.login(self.username, self.password)
            self.connected = True
            logger.info(f"✅ Successfully authenticated with Bluesky as {self.username}")
            return True
        except Exception as e:
            logger.error(f"❌ Error authenticating with Bluesky: {str(e)}")
            return False

    async def _construct_uri(self, url: str) -> str | None:
        """Extract post URI from the URL - Bluesky URLs typically look like:
        https://bsky.app/profile/username.bsky.social/post/abcdef123"""
        try:
            # Parse URL to extract components for building the AT URI
            parts = url.split('/')
            if len(parts) < 5 or "bsky.app" not in url:
                raise Exception(f"Invalid Bluesky URL format for {url}.")

            # Find the profile part of the URL
            profile_idx = -1
            for i, part in enumerate(parts):
                if part == "profile":
                    profile_idx = i
                    break

            if profile_idx < 0 or profile_idx + 3 >= len(parts):
                raise Exception(f"Could not extract profile or post ID from {url}.")

            handle = parts[profile_idx + 1]
            post_id = parts[profile_idx + 3]

            # Resolve the handle to a DID
            did = await self._resolve_handle(handle)

            # Construct the AT URI
            uri = f"at://{did}/app.bsky.feed.post/{post_id}"

            return uri

        except Exception as e:
            err_msg = error_to_string(e)
            logger.error(f"Error retrieving Bluesky post: {err_msg}")

    async def _resolve_handle(self, handle: str) -> str:
        """Resolve a handle to a DID."""
        try:
            response = await self.client.resolve_handle(handle)
            return response.did
        except Exception as e:
            err_msg = error_to_string(e)
            logger.error(f"Error resolving handle: {err_msg}")
            return handle  # Return the handle itself as fallback


def error_to_string(error: Exception) -> str:
    """Takes an Error object containing a response and prints the contents."""
    from atproto_client.exceptions import RequestErrorBase
    if isinstance(error, RequestErrorBase):
        response = error.response
        code = response.status_code
        content = response.content
        from atproto_client.models.common import XrpcError
        if isinstance(content, XrpcError):
            error_type = content.error
            msg = content.message
            return f"Error {code} ({error_type}): {msg}."
        else:
            return f"Error {code}: {content}."
    else:
        return str(error)
