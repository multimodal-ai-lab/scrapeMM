import asyncio
import logging
from typing import Optional
import subprocess
import tempfile
import os
import m3u8
from urllib.parse import urljoin

import aiohttp
from atproto import Client
from atproto_client.exceptions import RequestErrorBase
from atproto_client.models.common import XrpcError
from ezmm import MultimodalSequence, download_image
from ezmm.common.items import Video

from scrapemm.api_keys import get_api_key
from scrapemm.integrations.base import RetrievalIntegration
from scrapemm.util import get_domain

logger = logging.getLogger("scrapeMM")


class Bluesky(RetrievalIntegration):
    domains = ["bsky.app"]

    def __init__(self):
        self.username = get_api_key("bluesky_username")
        self.password = get_api_key("bluesky_password")

        if not (self.username and self.password):
            logger.warning("❌ Bluesky integration not configured: Missing username or password.")
            self.connected = False
            return
        
        self.client = Client()
        self.authenticated = False
        self._authenticate()

    async def get(self, url: str, session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
        if not self.authenticated:
            logger.error("❌ Not authenticated with Bluesky. Cannot retrieve content.")
            return None
        
        if get_domain(url) not in self.domains:
            logger.error(f"❌ Invalid domain for Bluesky: {get_domain(url)}")
            return None
        
        if "post" in url:
            result = await self._retrieve_post(url, session)
        else:
            result = await self._retrieve_profile(url, session)

        return result
    
    async def _retrieve_post(self, url: str, session: aiohttp.ClientSession = None) -> Optional[MultimodalSequence]:
        """Retrieve a post from the given Bluesky URL."""
        uri = self._construct_uri(url)
        if not uri:
            logger.error(f"❌ Could not construct URI for Bluesky post: {url}")
            return None

        try:
            thread_response = self.client.get_post_thread(uri=uri, depth=0, parent_height=0)
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
                    video = await download_hls_video(embed.playlist, session)
                    if video:
                        media.append(video)              

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
                    reply_to_post = self.client.get_posts([parent_uri]).posts[0]
                    self.n_api_calls += 1
                    reply_to_author = reply_to_post.author
                    reply_to = f"https://bsky.app/profile/{reply_to_author.handle}/post/{post_id}"

            text = f"""**Post on Bluesky**
Author: {author_display_name} (@{author_username})
Posted on: {created_at_str}
Likes: {like_count} - Comments: {comment_count} - Shares: {share_count}
{"Reply to: " + reply_to if is_reply and reply_to else ""}
{post_text}"""
            return MultimodalSequence([text, *media])

        except Exception as e:
            err_msg = error_to_string(e)
            logger.error(f"❌ Error retrieving Bluesky post: {err_msg}")
            return None


    async def _retrieve_profile(self, url: str, session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
        """Retrieve a profile from the given Bluesky URL."""
        profile = self.client.get_profile(url.split('/')[-1])

        if profile.avatar:
            avatar = await download_image(profile.avatar, session)

        if profile.banner:
            banner = await download_image(profile.banner, session)

        text = f"""**Profile on Bluesky**
User: {profile.display_name} (@{profile.handle})
Created on: {profile.created_at}
Profile image: {avatar.reference or 'None'}
Profile banner: {banner.reference or 'None'}

URL: {url}
Description: {profile.description or 'No description provided'}

Metrics:
- Follower count: {profile.followers_count}
- Following count: {profile.follows_count}
- Post count: {profile.posts_count}
            """
        return MultimodalSequence(text)

    def _authenticate(self) -> bool:
        """Authenticate with Bluesky using provided credentials."""
        try:
            self.client.login(self.username, self.password)
            self.authenticated = True
            logger.info(f"✅ Successfully authenticated with Bluesky as {self.username}")
            return True
        except Exception as e:
            logger.error(f"❌ Error authenticating with Bluesky: {str(e)}")
            return False

    def _construct_uri(self, url: str) -> str:
        # Extract post URI from the URL - Bluesky URLs typically look like:
        # https://bsky.app/profile/username.bsky.social/post/abcdef123
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
            did = self._resolve_handle(handle)

            # Construct the AT URI
            uri = f"at://{did}/app.bsky.feed.post/{post_id}"

            return uri

        except Exception as e:
            err_msg = error_to_string(e)
            logger.error(f"Error retrieving Bluesky post: {err_msg}")

    def _resolve_handle(self, handle: str) -> str:
        """Resolve a handle to a DID."""
        try:
            response = self.client.resolve_handle(handle)
            return response.did
        except Exception as e:
            err_msg = error_to_string(e)
            logger.error(f"Error resolving handle: {err_msg}")
            return handle  # Return the handle itself as fallback


def error_to_string(error: RequestErrorBase | Exception) -> str:
    """Takes an Error object containing a response and prints the contents."""
    if isinstance(error, RequestErrorBase):
        response = error.response
        code = response.status_code
        content = response.content
        if isinstance(content, XrpcError):
            error_type = content.error
            msg = content.message
            return f"Error {code} ({error_type}): {msg}."
        else:
            return f"Error {code}: {content}."
    else:
        return str(error)


async def download_hls_video(
        playlist_url: str,
        session: aiohttp.ClientSession
) -> Optional[Video]:
    """Download an HLS video from a playlist URL and return it as a Video object."""
    try:
        # Download the m3u8 playlist
        async with session.get(playlist_url) as response:
            if response.status != 200:
                print(f"Failed to download playlist: {response.status}")
                return None
            playlist_content = await response.text()
        
        playlist = m3u8.loads(playlist_content)
        
        # Check if this is a master playlist (contains variant playlists)
        if playlist.is_variant:
            # Choose the highest quality variant (or you could choose a specific one)
            best_playlist = playlist.playlists[-1]  # Usually the last one is highest quality
            
            # Manually construct the absolute URL for the variant playlist
            base_url = playlist_url.rsplit('/', 1)[0] + '/'
            variant_url = urljoin(base_url, best_playlist.uri)

            # Download the variant playlist
            async with session.get(variant_url) as var_response:
                if var_response.status != 200:
                    logger.error(f"Failed to download variant playlist: {var_response.status}")
                    return None
                variant_content = await var_response.text()

            # Parse the variant playlist
            variant_playlist = m3u8.loads(variant_content)
            playlist = variant_playlist  # Use this for segment downloads
            
            # Update base_url for segment downloads
            base_url = variant_url.rsplit('/', 1)[0] + '/'

        # Download all segments
        video_segments = []
        
        for i, segment in enumerate(playlist.segments):
            # Construct full URL for the segment
            if segment.uri.startswith('http'):
                segment_url = segment.uri
            else:
                segment_url = urljoin(base_url, segment.uri)

            # Download the segment with SSL disabled
            try:
                async with session.get(segment_url) as seg_response:
                    if seg_response.status == 200:
                        segment_data = await seg_response.read()
                        video_segments.append(segment_data)
            except Exception as e:
                logger.error(f"Failed to download segment {i} from {segment_url}: {e}")

        # Combine all segments
        if video_segments:
            combined_content = b''.join(video_segments)

            # Save as temporary TS file first
            with tempfile.NamedTemporaryFile(suffix='.ts', delete=False) as temp_ts:
                temp_ts.write(combined_content)
                temp_ts_path = temp_ts.name
            
            # Convert to MP4 using ffmpeg
            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_mp4:
                temp_mp4_path = temp_mp4.name
            
            try:
                # Use ffmpeg to convert TS to MP4
                subprocess.run([
                    'ffmpeg', '-i', temp_ts_path, 
                    '-c', 'copy',  # Copy streams without re-encoding
                    '-y',  # Overwrite output file
                    temp_mp4_path
                ], check=True, capture_output=True)
                
                # Read the converted MP4
                with open(temp_mp4_path, 'rb') as f:
                    mp4_content = f.read()

                # Clean up temp files
                os.unlink(temp_ts_path)
                os.unlink(temp_mp4_path)
                
                # Create Video object with MP4 content
                video = Video(binary_data=mp4_content, source_url=playlist_url)
                video.relocate(move_not_copy=True)
                return video
                
            except subprocess.CalledProcessError as e:
                logger.warning(f"ffmpeg conversion failed: {e}")
                # Fallback to TS format
                video = Video(binary_data=combined_content, source_url=playlist_url)
                video.relocate(move_not_copy=True)
                return video
            except FileNotFoundError:
                logger.warning("ffmpeg not found, saving as TS format.")
                # Fallback to TS format
                video = Video(binary_data=combined_content, source_url=playlist_url)
                video.relocate(move_not_copy=True)
                return video
        
    except Exception as e:
        logger.error(f"Error downloading HLS video from {playlist_url}: {e}")
        return None
    
    return None
