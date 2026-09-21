from typing import Optional, TYPE_CHECKING, Union
from urllib.parse import urljoin
import asyncio
import logging
import os
import shutil
from functools import lru_cache
from pathlib import Path

import aiohttp
import m3u8
from ezmm import Video
from ezmm.util import ts_to_mp4

from scrapemm.download.util import (looks_like_hls_url, looks_like_video_file_url,
                                    exceeds_max_size)

if TYPE_CHECKING:
    from playwright.async_api import APIRequestContext

from scrapemm.download.requests import fetch_headers, request_static
from scrapemm.download.common import HEADERS

logger = logging.getLogger("scrapeMM")

# HLS playlists routinely contain hundreds of segments. Fetch them in parallel, but
# keep the fan-out per video modest so one video cannot monopolise the connection pool.
MAX_CONCURRENT_SEGMENTS = 10


async def download_video(
        video_url: str,
        session: Union[aiohttp.ClientSession, "APIRequestContext"],
        max_video_size: Optional[int] = None,
        **kwargs
) -> Optional[Video]:
    """Downloads the linked video (stream) and returns it as a Video object.
    Videos larger than `max_video_size` bytes are skipped."""

    try:
        content_type = ''
        try:
            headers = await fetch_headers(video_url, session, **kwargs)
            content_type = headers.get('Content-Type') or headers.get('content-type') or ''

            # Skip oversized videos before downloading a single byte of them
            if exceeds_max_size(headers, max_video_size):
                logger.debug(f"Skipping {video_url}: larger than the limit of {max_video_size} bytes.")
                return None
        except Exception as e:
            # The probe only tells us *how* to download. Losing it is no reason to give
            # up on a URL that already looks like a video: slow or HEAD-hostile hosts
            # would otherwise cost us the media entirely.
            if not (looks_like_video_file_url(video_url) or looks_like_hls_url(video_url)):
                raise
            logger.debug(f"Header probe failed for {video_url} ({type(e).__name__}); "
                         f"falling back to the URL suffix.")

        if is_video(content_type) or looks_like_video_file_url(video_url):
            return await download_video_file(video_url, session, max_video_size=max_video_size, **kwargs)
        elif is_hls(content_type) or looks_like_hls_url(video_url):
            return await download_hls_video(video_url, session, max_video_size=max_video_size, **kwargs)
        else:
            logger.warning(
                f"Cannot download video from {video_url}. Unable to handle content type: {content_type}."
            )

    except Exception as e:
        logger.debug(f"Error downloading video from {video_url}", exc_info=e)


def is_video(content_type: str) -> bool:
    """Returns True iff the given content type is a video."""
    normalized_ct = content_type.split(';', 1)[0].strip().lower()
    return normalized_ct.startswith("video/")


def is_hls(content_type: str) -> bool:
    """Returns True iff the given content type is an HLS playlist."""
    normalized_ct = content_type.split(';', 1)[0].strip().lower()
    return (
            normalized_ct in ("application/vnd.apple.mpegurl", "application/x-mpegurl")
            or "mpegurl" in normalized_ct
    )


async def download_video_file(
        video_url: str,
        session: Union[aiohttp.ClientSession, "APIRequestContext"],
        max_video_size: Optional[int] = None,
        **kwargs
) -> Optional[Video]:
    """Download a single video file from a URL and return it as a Video object.
    The download is aborted once it exceeds `max_video_size` bytes."""
    try:
        content = await request_static(video_url, session, get_text=False,
                                       max_size=max_video_size, **kwargs)
        if content:
            assert isinstance(content, bytes)
            return video_from_binary(content, video_url)
        else:
            logger.debug(f"Failed to download video from {video_url}")
    except Exception as e:
        logger.debug(f"Error downloading video file from {video_url}"
                     f"\n{type(e).__name__}: {e}")


def video_from_binary(binary_data: bytes, source_url: str) -> Video:
    """Create a Video object from binary data."""
    video = Video(binary_data=binary_data, source_url=source_url)
    video.relocate(move_not_copy=True)
    return video


async def download_hls_video(
        playlist_url: str,
        session: Union[aiohttp.ClientSession, "APIRequestContext"],
        max_video_size: Optional[int] = None,
        **kwargs
) -> Optional[Video]:
    """Download an HTTP Live Streaming (HLS) video from a playlist URL and return it as a Video object."""
    try:
        variant_content = ""

        # Download the m3u8 playlist file
        playlist_content = await request_static(playlist_url, session, get_text=True, **kwargs)

        if not playlist_content:
            logger.debug(f"Failed to download playlist: {playlist_url}")
            return None

        playlist = m3u8.loads(playlist_content)
        base_url = playlist_url.rsplit('/', 1)[0] + '/'
        final_playlist_url = playlist_url

        # Check if this is a master playlist (contains variant playlists)
        if playlist.is_variant:
            # Choose the highest quality variant
            best_playlist = playlist.playlists[-1]  # Usually the last one is of highest quality

            # Manually construct the absolute URL for the variant playlist
            variant_url = urljoin(base_url, best_playlist.uri)

            # Download the variant playlist
            variant_content = await request_static(variant_url, session, get_text=True, **kwargs)

            if not variant_content:
                logger.error(f"Failed to download variant playlist: {variant_url}")
                return None

            # Parse the variant playlist
            variant_playlist = m3u8.loads(variant_content)
            playlist = variant_playlist  # Use this for segment downloads

            # Update base_url for segment downloads
            base_url = variant_url.rsplit('/', 1)[0] + '/'
            final_playlist_url = variant_url

        # Detect CMAF/fMP4 vs MPEG-TS. ffmpeg error reported indicates fragments are fMP4.
        # Heuristics: EXT-X-MAP present in playlist content or segment URIs ending with .m4s/.mp4
        content_to_check = playlist_content if final_playlist_url == playlist_url else variant_content
        is_cmaf = False
        try:
            if content_to_check and ('#EXT-X-MAP' in content_to_check):
                is_cmaf = True
        except NameError:
            pass
        if not is_cmaf:
            for seg in playlist.segments:
                uri = (seg.uri or '').lower()
                if uri.endswith('.m4s') or uri.endswith('.mp4') or uri.endswith('.cmfv'):
                    is_cmaf = True
                    break

        if is_cmaf:
            # Use ffmpeg to remux HLS (CMAF/fMP4) directly into MP4.
            mp4_bytes = await _ffmpeg_remux_hls_to_mp4(final_playlist_url)
            if mp4_bytes:
                video = Video(binary_data=mp4_bytes, source_url=playlist_url)
                video.relocate(move_not_copy=True)
                return video
            return None

        # Download all segments concurrently. A playlist routinely has hundreds of
        # them, so fetching one after another dominates the retrieval time.
        segment_urls = [
            segment.uri if segment.uri.startswith('http') else urljoin(base_url, segment.uri)
            for segment in playlist.segments
        ]
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_SEGMENTS)

        async def fetch_segment(index: int, segment_url: str) -> Optional[bytes]:
            async with semaphore:
                try:
                    return await request_static(segment_url, session, get_text=False)
                except Exception as e:
                    logger.debug(f"Failed to download segment {index} from {segment_url}: {e}")
                    return None

        downloaded = await asyncio.gather(
            *(fetch_segment(i, u) for i, u in enumerate(segment_urls))
        )
        # Keep playlist order; skipping failed segments matches the previous behaviour
        video_segments = [data for data in downloaded if data]

        if max_video_size is not None:
            total = sum(len(data) for data in video_segments)
            if total > max_video_size:
                logger.debug(f"Skipping HLS video {playlist_url}: {total} bytes exceed "
                             f"the limit of {max_video_size}.")
                return None

        # Combine all segments
        if video_segments:
            ts_bytes = b''.join(video_segments)
            mp4_bytes = ts_to_mp4(ts_bytes)

            # Create Video object with MP4 content
            video = Video(binary_data=mp4_bytes, source_url=playlist_url)
            video.relocate(move_not_copy=True)
            return video

    except Exception as e:
        logger.debug(f"Error downloading HLS video from {playlist_url}"
                     f"\n{type(e).__name__}: {e}")

    return None


async def is_maybe_video_url(url: str, session: Union[aiohttp.ClientSession, "APIRequestContext"]) -> bool:
    """Returns True iff the URL points at an accessible video file/stream."""
    try:
        headers = await fetch_headers(url, session)
        content_type = headers.get('Content-Type') or headers.get('content-type') or ''
        if content_type.startswith("video/") or content_type == "application/vnd.apple.mpegurl":
            # Surely a video
            return True
        else:
            # If the content is a binary download stream, use URL suffix heuristics.
            return content_type == "binary/octet-stream" and (
                    looks_like_video_file_url(url) or looks_like_hls_url(url)
            )

    except Exception:
        logger.debug(f"Error probing video URL {url}", exc_info=True)
        return False


async def _ffmpeg_remux_hls_to_mp4(playlist_url: str) -> Optional[bytes]:
    """Use FFmpeg to read an HLS playlist (CMAF/fMP4) and remux to MP4, returning bytes.

    We pass headers for basic compatibility and copy streams without re-encoding.
    """
    # Prepare optional headers for ffmpeg.
    user_agent = HEADERS.get('User-Agent', '')
    headers_lines = []
    # Some servers require Accept or similar; keep it minimal.
    if 'Accept' in HEADERS:
        headers_lines.append(f"Accept: {HEADERS['Accept']}")
    headers_arg = "\r\n".join(headers_lines) if headers_lines else None

    # Resolve ffmpeg executable path robustly
    ffmpeg_path = _resolve_ffmpeg_path()
    if not ffmpeg_path:
        logger.error("FFmpeg not found. Please install FFmpeg and ensure it is available in PATH, or set FFMPEG_PATH/IMAGEIO_FFMPEG_EXE.")
        return None

    cmd = [
        ffmpeg_path,
        '-loglevel', 'error',
        '-hide_banner',
    ]
    if user_agent:
        cmd += ['-user_agent', user_agent]
    if headers_arg:
        cmd += ['-headers', headers_arg]
    cmd += [
        '-i', playlist_url,
        '-c', 'copy',
        # MP4 muxer to non-seekable stdout requires fragmented MP4
        '-movflags', 'frag_keyframe+empty_moov',
        '-f', 'mp4',
        'pipe:1'
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0 and stdout:
            return stdout
        err = stderr.decode('utf-8', errors='ignore') if stderr else ''
        raise RuntimeError(f"FFmpeg error:\n{err}")
    except FileNotFoundError:
        logger.error("FFmpeg not found. Cannot remux HLS.")
    except Exception as e:
        logger.error(f"FFmpeg failed: {e}")
    return None


@lru_cache(maxsize=1)
def _resolve_ffprobe_path() -> Optional[str]:
    """Find an ffprobe executable. ffprobe ships alongside ffmpeg, so the ffmpeg
    location found by `_resolve_ffmpeg_path()` is the most reliable hint."""
    which = shutil.which("ffprobe")
    if which:
        return which

    ffmpeg = _resolve_ffmpeg_path()
    if ffmpeg:
        sibling = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
        if sibling.is_file():
            return str(sibling)

    return None


@lru_cache(maxsize=1)
def _resolve_ffmpeg_path() -> Optional[str]:
    """Find an FFmpeg executable path using env vars, PATH, common Windows locations,
    and optionally imageio-ffmpeg.

    Returns absolute path to ffmpeg executable or None if not found.
    """
    # 1) Explicit environment variables
    candidates = [
        os.environ.get('FFMPEG_PATH'),
        os.environ.get('FFMPEG_BIN'),
        os.environ.get('IMAGEIO_FFMPEG_EXE'),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c

    # 2) PATH lookup
    which = shutil.which('ffmpeg')
    if which:
        return which

    # 3) Common Windows install locations
    possible_dirs = []
    pf = os.environ.get('ProgramFiles')
    pf86 = os.environ.get('ProgramFiles(x86)')
    pf64 = os.environ.get('ProgramW6432')
    userprofile = os.environ.get('USERPROFILE')
    # Typical layouts
    for base in filter(None, {pf, pf86, pf64}):
        possible_dirs.extend([
            os.path.join(base, 'ffmpeg', 'bin', 'ffmpeg.exe'),
            os.path.join(base, 'FFmpeg', 'bin', 'ffmpeg.exe'),
        ])
    # Scoop shim
    if userprofile:
        possible_dirs.append(os.path.join(userprofile, 'scoop', 'shims', 'ffmpeg.exe'))
    # Chocolatey
    possible_dirs.append(r'C:\ProgramData\chocolatey\bin\ffmpeg.exe')

    for p in possible_dirs:
        if os.path.isfile(p):
            return p

    # 4) imageio-ffmpeg as last resort
    try:
        import imageio_ffmpeg  # type: ignore
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        logger.debug("imageio-ffmpeg not available for FFmpeg resolution", exc_info=True)

    return None
