import logging
from typing import Optional
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger("scrapeMM")

# Read bodies in large chunks. Media files are routinely tens of megabytes, where a
# small chunk size costs one event-loop round trip per few kilobytes.
DEFAULT_CHUNK_SIZE = 256 * 1024


class MediaTooLarge(Exception):
    """Raised when a download exceeds the permitted size and was therefore aborted."""


async def stream(response: aiohttp.ClientResponse,
                 chunk_size: int = DEFAULT_CHUNK_SIZE,
                 max_size: Optional[int] = None) -> bytes:
    """Reads the response body. If `max_size` is given, the download is aborted as
    soon as it exceeds that many bytes, raising `MediaTooLarge`."""
    data = bytearray()
    async for chunk in response.content.iter_chunked(chunk_size):
        data.extend(chunk)
        if max_size is not None and len(data) > max_size:
            raise MediaTooLarge(
                f"Download from {response.url} exceeds the limit of {max_size} bytes."
            )
    return bytes(data)  # Convert to immutable bytes if needed


def exceeds_max_size(headers: dict, max_size: Optional[int]) -> bool:
    """True iff the Content-Length header announces a body larger than `max_size`.
    Lets us skip oversized media without downloading a single byte of it."""
    if max_size is None:
        return False
    length = headers.get("Content-Length") or headers.get("content-length")
    try:
        return length is not None and int(length) > max_size
    except (TypeError, ValueError):
        return False


IMAGE_FILE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")  # Only pixel images
VECTOR_FILE_EXTENSIONS = (".svg", ".svgz", ".eps")
VIDEO_FILE_EXTENSIONS = (
    ".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".flv", ".wmv", ".ts"
)


def looks_like_image_file_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(IMAGE_FILE_EXTENSIONS)


def looks_like_vector_file_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(VECTOR_FILE_EXTENSIONS)


def looks_like_hls_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".m3u8")


def looks_like_video_file_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(VIDEO_FILE_EXTENSIONS)


# Hosts whose iframes embed a video that yt-dlp can download. Deliberately a short
# allowlist of actual video platforms: pages carry plenty of other iframes (ads, comment
# widgets, maps), and running yt-dlp on each of them would cost a lot of time for nothing.
VIDEO_EMBED_HOSTS = (
    "youtube.com/embed/",
    "youtube-nocookie.com/embed/",
    "youtu.be/",
    "player.vimeo.com/video/",
    "dailymotion.com/embed/",
    "facebook.com/plugins/video",
)


def looks_like_video_embed_url(url: str) -> bool:
    """True if the URL embeds a video player of a known video platform."""
    return any(host in url.lower() for host in VIDEO_EMBED_HOSTS)
