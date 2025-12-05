import asyncio
import base64
import binascii
import logging
import re
import sys
from typing import Optional, Awaitable, Iterable

import aiohttp
import tqdm
from PIL import UnidentifiedImageError
from ezmm import MultimodalSequence, download_item, Item, Image, Video
from markdownify import markdownify as md

logger = logging.getLogger("scrapeMM")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/123.0.0.0 Safari/537.36",
}

DOMAIN_REGEX = r"(?:https?:\/\/)?(?:www\.)?([-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6})/?"


def get_domain(url: str, keep_subdomain: bool = False) -> Optional[str]:
    """Uses regex to get out the domain from the given URL. The output will be
    of the form 'example.com'. No 'www', no 'http'."""
    url = str(url)
    match = re.search(DOMAIN_REGEX, url)
    if match:
        domain = match.group(1)
        if not keep_subdomain:
            # Keep only second-level and top-level domain
            domain = '.'.join(domain.split('.')[-2:])
        return domain


async def run_with_semaphore(tasks: Iterable[Awaitable],
                             limit: int,
                             show_progress: bool = True,
                             progress_description: str | None = None) -> list:
    """
    Runs asynchronous tasks with a concurrency limit.

    Args:
        tasks: The tasks to execute concurrently.
        limit: The maximum number of coroutines to run concurrently.
        show_progress: Whether to show a progress bar while executing tasks.
        progress_description: The message to display in the progress bar.

    Returns:
        list: A list of results returned by the tasks, order-preserved.
    """
    semaphore = asyncio.Semaphore(limit)  # Limit concurrent executions

    async def limited_coroutine(t: Awaitable):
        async with semaphore:
            return await t

    print(progress_description, end="\r")

    tasks = [asyncio.create_task(limited_coroutine(task)) for task in tasks]

    # Report completion status of tasks (if more than one task)
    if show_progress:
        progress = tqdm.tqdm(total=len(tasks), desc=progress_description, file=sys.stdout)
        while progress.n < len(tasks):
            progress.n = sum(task.done() for task in tasks)
            progress.refresh()
            await asyncio.sleep(0.1)
        progress.close()

    return await asyncio.gather(*tasks)


def read_urls_from_file(file_path):
    with open(file_path, 'r') as f:
        return f.read().splitlines()


def get_multiline_user_input(prompt: str) -> str:
    print(prompt)
    lines = sys.stdin.readlines()
    return "".join(lines)


MAX_MEDIA_PER_PAGE = 32
URL_REGEX = r"https?:\/\/(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9@:%_\+.~#?&//=]*)"
DATA_URI_REGEX = r"data:([\w/+.-]+/[\w.+-]+);base64,([A-Za-z0-9+/=]+)"
MD_HYPERLINK_REGEX = rf'(!?\[([^]^[]*)\]\((.*?)(?: "[^"]*")?\))'


def postprocess_scraped(text: str) -> str:
    # Remove any excess whitespaces
    text = re.sub(r' {2,}', ' ', text)

    # Remove any excess newlines
    text = re.sub(r'(\n *){3,}', '\n\n', text)

    return sanitize(text.strip())


async def resolve_media_hyperlinks(
        text: str, session: aiohttp.ClientSession,
        domain_root: str | None = None,
        remove_urls: bool = False,
) -> Optional[MultimodalSequence]:
    """Downloads all media that are hyperlinked in the provided Markdown text.
    Only considers images with substantial size (larger than 256 x 256) and replaces the
    respective Markdown hyperlinks with their proper image reference."""

    if text is None:
        return None

    # Extract URLs and base64-encoded data from the text
    hyperlinks = get_markdown_hyperlinks(text)
    hrefs_urls = dict()
    data_uris = set()
    for _, _, href in hyperlinks:
        if is_url(href):
            hrefs_urls[href] = href
        elif domain_root and is_root_relative_url(href):
            hrefs_urls[href] = f"{domain_root}{href}"
        elif is_data_uri(href):
            data_uris.add(href)

    # Try to download media for each URL
    tasks = [download_item(url, session=session) for url in hrefs_urls.values()]
    media: list[Item | None] = await run_with_semaphore(tasks, limit=100, show_progress=False)

    href_media = dict(zip(hrefs_urls.keys(), media))

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
    """Returns True iff the given string is an absolute HTTP URL."""
    return re.match(URL_REGEX, href) is not None


def is_root_relative_url(href: str) -> bool:
    """Returns True iff the given string is a root-relative URL."""
    return href.startswith("/")


def is_data_uri(href: str) -> bool:
    """Returns True iff the given string is a valid data URI."""
    return re.match(DATA_URI_REGEX, href) is not None


def get_domain_root(url: str) -> Optional[str]:
    """Extracts the domain root from the given URL. Allows for missing http(s) prefix."""
    match = re.match(r"(:?https?://)?([^/]+)", url)
    if match:
        return match.group(0)
    else:
        return None


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
        return None
    except UnidentifiedImageError:  # Pillow could not identify image format
        return None
    except Exception as e:
        logger.debug(f"Error decoding {mime_type} base64 data. \n {type(e).__name__}: {e}")
