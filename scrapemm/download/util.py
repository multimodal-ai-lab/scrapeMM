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
