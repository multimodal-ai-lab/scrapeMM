"""Integration for Archive.today retrieval."""

import logging
import requests

import aiohttp
from ezmm import MultimodalSequence

from scrapemm.download.common import HEADERS
from scrapemm.integrations.base import RetrievalIntegration
from scrapemm.util import to_multimodal_sequence

from bs4 import BeautifulSoup

logger = logging.getLogger("scrapeMM")

MEDIA_VAULT_CONTENT_CLASS = "archive-item--boxed"
# MEDIA_VAULT_AUTHOR_DISPLAY_NAME_CLASS = "archive-item--boxed"
# MEDIA_VAULT_AUTHOR_USERNAME_CLASS = "archive-item--boxed"
# MEDIA_VAULT_AUTHOR_METADATA_CLASS = "archive-item--boxed"


class MediaVault(RetrievalIntegration):
    name = "MediaVault"
    domains = ["mvau.lt"]

    async def _connect(self):
        self.connected = True

    async def _get(self, url: str, **kwargs) -> MultimodalSequence | None:
        archived_content_html = await self.get_record_html(url)
        if archived_content_html is not None:
            async with aiohttp.ClientSession(headers=HEADERS) as session:
                return await to_multimodal_sequence(
                    archived_content_html, remove_urls=False, session=session, url=url
                )
        else:
            raise RuntimeError("Failed to retrieve MediaVault record HTML.")

    async def get_record_html(self, url: str) -> str | None:
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
            logger.error(f"Failed to retrieve page. Status code: {response.status_code}")
            return None
        
        logger.info(f"Successfully retrieved page with url: {url}")

        soup = BeautifulSoup(response.text, 'html.parser')
        archive_item = soup.find('div', class_=MEDIA_VAULT_CONTENT_CLASS)

        if archive_item is None:
            logger.error(f"Could not find the '{MEDIA_VAULT_CONTENT_CLASS}' element in the page. Returning the whole HTML content instead.")
            return response.text

        return self._normalize_media_html(archive_item.decode_contents())

    @staticmethod
    def _normalize_media_html(html: str) -> str:
        """Normalize MediaVault media tags to improve markdown media detection.

        MediaVault video blocks can include poster imagery/fallback text inside
        <video> tags. These often get converted to image links during markdown
        conversion. Replacing each video block with a direct link to its source
        preserves intended video retrieval.
        """
        soup = BeautifulSoup(html, "html.parser")
        for video_tag in soup.find_all("video"):
            source_tag = video_tag.find("source", src=True)
            source_url = source_tag.get("src") if source_tag else video_tag.get("src")
            if source_url:
                replacement = soup.new_tag("a", href=str(source_url))
                replacement.string = "Video"
                video_tag.replace_with(replacement)

        return str(soup)