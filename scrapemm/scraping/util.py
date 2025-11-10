import base64
import binascii
import logging
import re
from typing import Optional

import aiohttp
import requests
from ezmm import MultimodalSequence, download_item, Item, Image, Video
from markdownify import markdownify as md
from requests.exceptions import ReadTimeout, ConnectionError, RetryError

from scrapemm.util import run_with_semaphore

MAX_MEDIA_PER_PAGE = 32

URL_REGEX = r"https?:\/\/(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9@:%_\+.~#?&//=]*)"
DATA_URI_REGEX = r"data:([\w/+.-]+/[\w.+-]+);base64,([A-Za-z0-9+/=]+)"
MD_HYPERLINK_REGEX = rf'(!?\[([^]^[]*)\]\((.*?)(?: "[^"]*")?\))'

logger = logging.getLogger("scrapeMM")


def find_firecrawl(urls):
    for url in urls:
        if firecrawl_is_running(url):
            return url
    return None


def firecrawl_is_running(url: str) -> bool:
    """Returns True iff Firecrawl is running at the specified URL."""
    if not url:
        return False
    try:
        if not url.startswith("http"):
            url = "https://" + url
        response = requests.get(url, timeout=0.2)
    except (ConnectionError, RetryError, ReadTimeout):
        return False
    return response.status_code == 200


def postprocess_scraped(text: str) -> str:
    # Remove any excess whitespaces
    text = re.sub(r' {2,}', ' ', text)

    # Remove any excess newlines
    text = re.sub(r'(\n *){3,}', '\n\n', text)

    return sanitize(text.strip())


async def resolve_media_hyperlinks(
        text: str, session: aiohttp.ClientSession,
        remove_urls: bool = False,
) -> Optional[MultimodalSequence]:
    """Downloads all media that are hyperlinked in the provided Markdown text.
    Only considers images with substantial size (larger than 256 x 256) and replaces the
    respective Markdown hyperlinks with their proper image reference."""

    if text is None:
        return None

    # Extract URLs and base64-encoded data from the text
    hyperlinks = get_markdown_hyperlinks(text)
    urls = set()
    data_uris = set()
    for _, _, href in hyperlinks:
        if is_url(href):
            urls.add(href)
        elif is_data_uri(href):
            data_uris.add(href)

    # Try to download media for each URL
    tasks = [download_item(url, session=session) for url in urls]
    media: list[Item | None] = await run_with_semaphore(tasks, limit=100, show_progress=False)

    href_media = dict(zip(urls, media))

    # Convert each base64-encoded data to the respective medium
    for data_uri in data_uris:
        mime_type, base64_encoding = decompose_data_uri(data_uri)
        href_media[data_uri] = from_base64(base64_encoding, mime_type=mime_type)

    # Replace hyperlinks with their respective media reference
    media_count = 0
    for full_match, hypertext, href in hyperlinks:
        medium = href_media.get(href)
        if medium:
            # Ignore small images
            to_ignore = isinstance(medium, Image) and (medium.width < 256 or medium.height < 256)
            reference = "" if to_ignore else medium.reference
            replacement = f"{hypertext} {reference}" if hypertext else reference
            text = text.replace(full_match, replacement)
            media_count += 1 if not to_ignore else 0
        elif remove_urls:
            text = text.replace(full_match, hypertext)

    return MultimodalSequence(text)


def is_url(href: str) -> bool:
    """Returns True iff the given string is a valid URL."""
    return re.match(URL_REGEX, href) is not None


def is_data_uri(href: str) -> bool:
    """Returns True iff the given string is a valid data URI."""
    return re.match(DATA_URI_REGEX, href) is not None


def get_markdown_hyperlinks(text: str) -> list[tuple[str, str, str]]:
    """Extracts all web hyperlinks from the given markdown-formatted string. Returns
    a list of fullmatch-hypertext-URL-triples."""
    pattern = re.compile(MD_HYPERLINK_REGEX, re.DOTALL)
    hyperlinks = re.findall(pattern, text)
    return hyperlinks


def decompose_data_uri(href: str) -> Optional[tuple[str, str]]:
    """Extracts the mime type and base64-encoded data from a data URI."""
    match = re.match(DATA_URI_REGEX, href)
    if match:
        return match.group(1), match.group(2)
    else:
        return None


async def to_multimodal_sequence(
        html: str | None,
        **kwargs
) -> Optional[MultimodalSequence]:
    """Turns a scraped output into the corresponding MultimodalSequences
    by converting the HTML into Markdown and resolving media hyperlinks."""
    try:
        text = md(html, heading_style="ATX")
    except RecursionError as e:
        return None

    text = postprocess_scraped(text)
    return await resolve_media_hyperlinks(text, **kwargs)


def sanitize(text: str) -> str:
    """Post-processes scraped text, removing invalid characters."""
    return text.replace("\u0000", "")


def from_base64(b64_data: str, mime_type: str = "image/jpeg") -> Optional[Item]:
    """Converts a base64-encoded image to an Item object."""
    try:
        binary_data = base64.b64decode(b64_data, validate=True)
        if binary_data:
            if mime_type.startswith("image/"):
                return Image(binary_data=binary_data)
            elif mime_type.startswith("video/"):
                return Video(binary_data=binary_data)
            else:
                raise ValueError(f"Unsupported media type: {mime_type}")
    except binascii.Error:  # base64 validation failed
        return
    except Exception as e:
        logger.warning(f"Error decoding base64 data!\n"
                       f"{type(e).__name__}: {e}")
