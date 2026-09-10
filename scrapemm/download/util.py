from urllib.parse import urlparse

import aiohttp


async def stream(response: aiohttp.ClientResponse, chunk_size: int = 1024) -> bytes:
    data = bytearray()
    async for chunk in response.content.iter_chunked(chunk_size):
        data.extend(chunk)
    return bytes(data)  # Convert to immutable bytes if needed


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
