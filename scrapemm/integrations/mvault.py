"""Integration for MediaVault (mvau.lt) retrieval."""

import logging
from typing import Any

import aiohttp
import requests
from bs4 import BeautifulSoup
from bs4.element import NavigableString
from ezmm import MultimodalSequence

from scrapemm.download.common import HEADERS
from scrapemm.download.media import download_medium
from scrapemm.integrations.base import RetrievalIntegration
from scrapemm.util import to_multimodal_sequence

logger = logging.getLogger("scrapeMM")

MEDIA_VAULT_CONTENT_CLASS = "archive-item--boxed"
MEDIA_VAULT_PUBLISHING_PLATFORM_SELECTOR = ".archive-item__metadatum__label"
MEDIA_VAULT_PUBLISHED_TIME_SELECTOR = (
    ".icon-prefixed time.archive-item__metadatum__value"
)
MEDIA_VAULT_AUTHOR_DISPLAY_NAME_SELECTOR = ".author__display-name"
MEDIA_VAULT_AUTHOR_USERNAME_SELECTOR = ".author__username"
MEDIA_VAULT_POST_URL_SELECTOR = ".archive-item__metadatum__value.break-all"
MEDIA_VAULT_CAPTION_SELECTOR = ".archive-item__body-caption-content"
MEDIA_VAULT_MEDIA_SELECTOR = ".archive-item__body-media"


class MediaVault(RetrievalIntegration):
    name = "MediaVault"
    domains = ["mvau.lt"]

    async def _connect(self):
        self.connected = True

    async def _get(self, url: str, **kwargs) -> MultimodalSequence | None:
        archived_content_html = await self._get_record_html(url)
        if archived_content_html is not None:
            async with aiohttp.ClientSession(headers=HEADERS) as session:
                multimodal_sequence = await self._custom_to_multimodal_sequence(
                    archived_content_html, session=session, url=url
                )

                if multimodal_sequence is not None:
                    return multimodal_sequence

                return await to_multimodal_sequence(
                    archived_content_html, remove_urls=False, session=session, url=url
                )
        else:
            raise RuntimeError("Failed to retrieve MediaVault record HTML.")

    async def _get_record_html(self, url: str) -> str | None:
        """
        Retrieves the HTML content of the archived web page.

        Args:
            url (str): The URL of the archived page on MediaVault.
        Returns:
            str | None: The HTML content of the archived page, or None if retrieval fails.
        """
        headers = {"User-Agent": HEADERS["User-Agent"]}
        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            logger.error(
                f"Failed to retrieve page. Status code: {response.status_code}"
            )
            return None

        logger.info(f"Successfully retrieved page with url: {url}")

        soup = BeautifulSoup(response.text, "html.parser")
        archive_item = soup.find("div", class_=MEDIA_VAULT_CONTENT_CLASS)

        if archive_item is None:
            logger.error(
                f"Could not find the '{MEDIA_VAULT_CONTENT_CLASS}' element in the page. Returning the whole HTML content instead."
            )
            return response.text

        return self._normalize_media_html(archive_item.decode_contents())

    @staticmethod
    def _normalize_media_html(html: str) -> str:
        """
        MediaVault video blocks include poster images/fallback text inside
        <video> tags. This function removes poster and fallback content
        but preserves <video>/<source>.
        """
        soup = BeautifulSoup(html, "html.parser")
        for video_tag in soup.find_all("video"):
            if video_tag.has_attr("poster"):
                del video_tag["poster"]

            for child in list(video_tag.contents):
                if isinstance(child, NavigableString):
                    child.extract()
                elif getattr(child, "name", None) != "source":
                    child.decompose()

        return str(soup)

    async def _custom_to_multimodal_sequence(
        self, record_html: str, session: aiohttp.ClientSession, url: str
    ) -> MultimodalSequence | None:
        """Custom parsing of MediaVault archived content into MultimodalSequence."""
        soup = BeautifulSoup(record_html, "html.parser")

        def _as_string(value: Any) -> str | None:
            return value if isinstance(value, str) else None

        # Metadata tags
        publishing_platform = soup.select_one(MEDIA_VAULT_PUBLISHING_PLATFORM_SELECTOR)
        published_time_tag = soup.select_one(MEDIA_VAULT_PUBLISHED_TIME_SELECTOR)
        author_tag = soup.select_one(MEDIA_VAULT_AUTHOR_DISPLAY_NAME_SELECTOR)
        username_tag = soup.select_one(MEDIA_VAULT_AUTHOR_USERNAME_SELECTOR)
        post_url_tag = soup.select_one(MEDIA_VAULT_POST_URL_SELECTOR)

        # Archived Item tags
        caption_tag = soup.select_one(MEDIA_VAULT_CAPTION_SELECTOR)
        media_tag = soup.select_one(MEDIA_VAULT_MEDIA_SELECTOR)

        # If any of the tags is not available, we return None
        # and fallback to the default to_multimodal_sequence parsing
        if not (
            publishing_platform
            and published_time_tag
            and author_tag
            and username_tag
            and post_url_tag
            and caption_tag
            and media_tag
        ):
            logger.debug(
                "Failed to manually parse MediaVault record. Falling back to default parsing for the whole HTML content."
            )            
            return None

        publishing_platform = publishing_platform.get_text(" ", strip=True).title()
        publishing_time = published_time_tag.get_text(" ", strip=True)
        author = author_tag.get_text(" ", strip=True)
        username = username_tag.get_text(" ", strip=True)
        post_url = post_url_tag.get_text(" ", strip=True)
        caption = caption_tag.get_text("\n", strip=True)

        # Looking for images
        posted_medium_url = None
        img = media_tag.find("img")
        if img:
            posted_medium_url = _as_string(img.get("src"))

        # Looking for videos if no image was available
        if posted_medium_url is None:
            video_tag = media_tag.find("video")
            if video_tag:
                posted_medium_url = _as_string(video_tag.get("src"))
                if posted_medium_url is None:
                    source_tag = video_tag.find("source", src=True)
                    posted_medium_url = _as_string(source_tag.get("src")) if source_tag else None

        # Download image/video
        posted_medium = None
        if posted_medium_url is not None:
            posted_medium = await download_medium(
                posted_medium_url,
                session=session,
                ignore_small_images=False,
                headers={"Referer": url},
            )

        if posted_medium is None:
            logger.debug(
                "Failed to manually parse MediaVault record. Falling back to default parsing for the whole HTML content."
            )               
            return None

        text = f"""
{publishing_platform}
Publishing date: {publishing_time}
Original post URL: {post_url}

Author: {author}
Username: {username}

Posted media: {posted_medium.reference}


Caption: 
{caption or "None"}
""".strip()

        items = [text]

        return MultimodalSequence(items)
