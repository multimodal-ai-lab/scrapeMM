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
                self.client.start(bot_token=bot_token)
            except sqlite3.OperationalError:  # Database is locked from an interrupted previous session
                # Remove the database file and try again
                journal_path = Path(self.session_path + ".session-journal")
                journal_path.unlink(missing_ok=True)
            self.connected = True
            logger.info("✅ Successfully connected to Telegram.")
        else:
            self.connected = False
            logger.warning("❌ Telegram integration not configured: Missing API keys.")

    async def _get(self, url: str, session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
        """Retrieves content from a Telegram post URL."""
        assert get_domain(url) in self.domains

        # Parse the URL to get channel/group name and post ID
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")

        if len(path_parts) < 2:
            return None

        channel_name = path_parts[0]
        post_id = int(path_parts[1])

        try:
            # Get the message
            channel = await self.client.get_entity(channel_name)
            message = await self.client.get_messages(channel, ids=post_id)

            if not message:
                return None

            # Handle media
            media = await self._get_media_from_message(channel, message)

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

        except Exception as e:
            logger.warning(f"Error retrieving Telegram content: {e}")
            return None

    async def _get_media_from_message(self, chat, original_post, max_amp=10) -> list[Item]:
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
                    else:
                        raise ValueError(f"Unsupported medium: {medium.__dict__}")
                    media.append(item)

        return media
