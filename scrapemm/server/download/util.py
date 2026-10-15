import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional
from urllib.parse import urlparse

import aiohttp

from scrapemm.common.scraping_response import OmittedMedium

logger = logging.getLogger("scrapeMM")

# The media a method's attempt left out (see `report_omitted()`): set by the engine per
# attempt, so a method that loses the race reports nothing into the winner's result.
# The list is shared, not copied, with the tasks and threads the attempt starts.
_omitted: ContextVar[Optional[list[OmittedMedium]]] = ContextVar("scrapemm_omitted_media",
                                                                  default=None)


@contextmanager
def collecting_omitted() -> Iterator[list[OmittedMedium]]:
    """Collects the media left out within this block, for the content's `omitted_media`."""
    token = _omitted.set(collected := [])
    try:
        yield collected
    finally:
        _omitted.reset(token)


def media_kind(content_type: Optional[str]) -> str:
    """"image" for an image's content type, else "video" (the size limits are for videos)."""
    return "image" if (content_type or "").lower().startswith("image/") else "video"


def report_omitted(url: str, limit: Optional[int], size: Optional[int] = None,
                   kind: str = "video") -> None:
    """Records that the medium at `url` was left out for exceeding the size `limit`;
    `size` if known (None if its download was stopped at the limit). Once per URL."""
    collected = _omitted.get()
    if collected is not None and all(medium.url != url for medium in collected):
        collected.append(OmittedMedium(url=url, kind=kind, size=size, limit=limit))

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
            report_omitted(str(response.url), max_size,
                           kind=media_kind(response.headers.get("Content-Type")))
            raise MediaTooLarge(
                f"Download from {response.url} exceeds the limit of {max_size} bytes."
            )
    return bytes(data)  # Convert to immutable bytes if needed


def exceeds_max_size(headers: dict, max_size: Optional[int], url: Optional[str] = None) -> bool:
    """True iff the Content-Length header announces a body larger than `max_size`.
    Lets us skip oversized media without downloading a single byte of it. With the
    medium's `url`, an oversized one is reported as left out (see `report_omitted()`)."""
    if max_size is None:
        return False
    length = headers.get("Content-Length") or headers.get("content-length")
    try:
        too_large = length is not None and int(length) > max_size
    except (TypeError, ValueError):
        return False
    if too_large and url:
        report_omitted(url, max_size, int(length),
                       kind=media_kind(headers.get("Content-Type") or headers.get("content-type")))
    return too_large


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


def looks_like_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


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
