import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from ezmm import MultimodalSequence, Image, Item, Video
from telethon import TelegramClient
from telethon.tl.types import Channel, User

from config import telegram_api_id, telegram_api_hash, telegram_bot_token
from integrations.base import RetrievalIntegration
from util import get_domain

logger = logging.getLogger("Retriever")


class Telegram(RetrievalIntegration):
    """The Telegram integration for retrieving post contents from Telegram channels and groups."""

    domains = ["t.me", "telegram.me"]
    session_path = "temp/telegram"

    def __init__(self):
        self.api_id = telegram_api_id
        self.api_hash = telegram_api_hash
        self.client = TelegramClient(self.session_path, self.api_id, self.api_hash)

    async def get(self, url: str, session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
        """Retrieves content from a Telegram post URL."""
        assert get_domain(url) in self.domains

        if not self.client.is_connected():
            logger.info("Connecting to Telegram...")
            await self.client.start(bot_token=telegram_bot_token)
            logger.info("✅ Successfully connected to Telegram...")

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
            print(f"Error retrieving Telegram content: {e}")
            # raise
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
                    medium_bytes = await self.client.download_media(post, file=bytes)
                    if hasattr(medium, "photo"):
                        item = Image(binary_data=medium_bytes)
                    elif hasattr(medium, "video"):
                        item = Video(binary_data=medium_bytes)
                    else:
                        raise ValueError(f"Unsupported medium: {medium.__dict__}")
                    media.append(item)

        return media


if __name__ == "__main__":
    urls = [
        "https://t.me/durov/404",  # one image
        "https://t.me/tglobaleye/16172",  # multiple images
        "https://t.me/tglobaleye/16178",  # video and quote
        "https://t.me/tglobaleye/6289",  # gif (treated as video)
        "https://t.me/tglobaleye/16192",  # images and video
    ]

    telegram = Telegram()
    for url in urls:
        task = telegram.get(url)
        out = asyncio.run(task)
        print(out, end="\n\n")
