import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
import sqlite3

import aiohttp
from ezmm import MultimodalSequence, Image, Item, Video
from telethon import TelegramClient
from telethon.tl.types import Channel, User

from scrapemm.secrets import get_secret
from scrapemm.integrations.base import RetrievalIntegration
from scrapemm.util import get_domain

logger = logging.getLogger("scrapeMM")


class Telegram(RetrievalIntegration):
    """The Telegram integration for retrieving post contents from Telegram channels and groups."""

    name = "Telegram"
    domains = ["t.me", "telegram.me"]
    session_path = "temp/telegram"

    async def _connect(self):
        api_id = int(get_secret("telegram_api_id")) if get_secret("telegram_api_id") else None
        api_hash = get_secret("telegram_api_hash")
        bot_token = get_secret("telegram_bot_token")

        if api_id and api_hash and bot_token:
            self.client = TelegramClient(self.session_path, api_id, api_hash)
            try:
                await self.client.start(bot_token=bot_token)  # Returns a coroutine b/c event loop exists already
            except sqlite3.OperationalError:  # Database is locked from an interrupted previous session
                # Remove the database file and try again
                journal_path = Path(self.session_path + ".session-journal")
                journal_path.unlink(missing_ok=True)
                await self.client.start(bot_token=bot_token)  # Returns a coroutine b/c event loop exists already
            self.connected = True
            logger.info("✅ Successfully connected to Telegram.")
        else:
            self.connected = False
            logger.warning("❌ Telegram integration not configured: Missing API keys.")

    async def _get(self, url: str, **kwargs) -> Optional[MultimodalSequence]:
        """Retrieves content from a Telegram post URL."""
        # Parse the URL to get channel/group name and post ID
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")

        if len(path_parts) < 2:
            return None

        channel_name = path_parts[0]
        post_id = int(path_parts[1])

        # Get the message
        channel = await self.client.get_entity(channel_name)
        if not channel:
            return None

        assert isinstance(channel, Channel), f"Can only retrieve posts from Telegram channels, but got {type(channel).__name__}."

        message = await self.client.get_messages(channel, ids=post_id)
        if not message:
            return None

        # Handle media
        max_video_size = kwargs.get("max_video_size")
        media = await self._get_media_from_message(channel, message, max_video_size=max_video_size)

        author = message.sender
        author_type = type(author).__name__
        if isinstance(author, Channel):
            name = f'"{author.title}"'
            if author.username:
                name += f" (@{author.username})"
        elif isinstance(author, User):
            if author.bot:
                author_type = "Bot"
            name = f"{author.first_name} {author.last_name}" if author.last_name else author.first_name
            if author.username:
                name += f" (@{author.username})"
            if author.phone:
                name += f", Phone: {author.phone}"
            if author.verified:
                name += " (Verified)"
        else:
            name = "Unknown"

        edit_text = "\nEdit date: " + message.edit_date.strftime("%B %d, %Y at %H:%M") if message.edit_date else ""
        reactions_text = "\nReactions: " + message.reactions.stringify() if message.reactions else ""

        text = f"""**Telegram Post**
Author: {author_type} {name}
Date: {message.date.strftime("%B %d, %Y at %H:%M")}{edit_text}
Views: {message.views}
Forwards: {message.forwards}{reactions_text}

{' '.join(m.reference for m in media)}
{message.text}"""

        return MultimodalSequence(text)

    async def _get_media_from_message(self, chat, original_post, max_amp=10, max_video_size=None) -> list[Item]:
        """
        Searches for Telegram posts that are part of the same group of uploads.
        The search is conducted around the id of the original post with an amplitude
        of `max_amp` both ways.
        Returns a list of [post] where each post has media and is in the same grouped_id.
        """
        # Gather posts that may belong to the same group
        if original_post.grouped_id is None:
            posts = [original_post]
        else:
            search_ids = list(range(original_post.id - max_amp, original_post.id + max_amp + 1))
            posts = await self.client.get_messages(chat, ids=search_ids)

        # Download media of posts that belong to the same group
        media = []
        for post in posts:
            if post is not None and post.grouped_id == original_post.grouped_id:
                if medium := post.media:
                    post_url = f"https://t.me/{chat.username}/{post.id}"
                    medium_bytes = await self.client.download_media(post, file=bytes)
                    if hasattr(medium, "photo"):
                        item = Image(binary_data=medium_bytes, source_url=post_url)
                    elif hasattr(medium, "video"):
                        item = Video(binary_data=medium_bytes, source_url=post_url)
                        if max_video_size is not None and item.size > max_video_size:
                            logger.info(f"Removing video {item.reference} because it exceeds the maximum size "
                                        f"of {max_video_size / 1024 / 1024:.2f} MB.")
                            item = None
                    else:
                        raise ValueError(f"Unsupported medium: {medium.__dict__}")
                    media.append(item)

        return media
