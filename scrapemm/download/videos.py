from typing import Optional
from urllib.parse import urljoin
import logging
import os
import shutil
from functools import lru_cache

import aiohttp
import m3u8
from ezmm import Video
from ezmm.util import ts_to_mp4

from scrapemm.download.requests import fetch_headers
from scrapemm.download.common import HEADERS, ssl_context

logger = logging.getLogger("scrapeMM")


async def download_video(
        video_url: str,
        session: aiohttp.ClientSession
) -> Optional[Video]:
    """Downloads the linked video (stream) and returns it as a Video object."""

    try:
        headers = await fetch_headers(video_url, session, timeout=3)
        content_type = headers.get('Content-Type') or headers.get('content-type') or ''
        # Normalize for robust detection
        normalized_ct = content_type.split(';', 1)[0].strip().lower()
        # TODO: Handle binary/octet-stream
        if normalized_ct.startswith("video/"):
            return await download_video_file(video_url, session)
        elif (
            normalized_ct in ("application/vnd.apple.mpegurl", "application/x-mpegurl")
            or "mpegurl" in normalized_ct
            or video_url.lower().endswith(".m3u8")
        ):
            return await download_hls_video(video_url, session)
        else:
            logger.warning(
                f"Cannot download video from {video_url}. Unable to handle content type: {content_type}."
            )

    except Exception as e:
        logger.debug(f"Error downloading video from {video_url}"
                     f"\n{type(e).__name__}: {e}")


async def download_video_file(
        video_url: str,
        session: aiohttp.ClientSession
) -> Optional[Video]:
    """Download a single video file from a URL and return it as a Video object."""
    try:
        async with session.get(video_url, ssl=ssl_context) as response:
            if response.status == 200:
                content = await response.read()
                video = Video(binary_data=content, source_url=video_url)
                video.relocate(move_not_copy=True)
                return video
            else:
                logger.debug(f"Failed to download video. {response.status}: {response.reason}")
    except Exception as e:
        logger.debug(f"Error downloading video file from {video_url}"
                     f"\n{type(e).__name__}: {e}")


async def download_hls_video(
        playlist_url: str,
        session: aiohttp.ClientSession
) -> Optional[Video]:
    """Download an HTTP Live Streaming (HLS) video from a playlist URL and return it as a Video object."""
    try:
        # Download the m3u8 playlist file
        async with session.get(playlist_url, ssl=ssl_context) as response:
            if response.status != 200:
                logger.debug(f"Failed to download playlist: {response.status}")
                return None
            playlist_content = await response.text()

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
            async with session.get(variant_url, ssl=ssl_context) as var_response:
                if var_response.status != 200:
                    logger.error(f"Failed to download variant playlist: {var_response.status}")
                    return None
                variant_content = await var_response.text()

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

        # Download all segments
        video_segments = []

        for i, segment in enumerate(playlist.segments):
            # Construct full URL for the segment
            if segment.uri.startswith('http'):
                segment_url = segment.uri
            else:
                segment_url = urljoin(base_url, segment.uri)

            # Download the segment with SSL disabled
            try:
                async with session.get(segment_url, ssl=ssl_context) as seg_response:
                    if seg_response.status == 200:
                        segment_data = await seg_response.read()
                        video_segments.append(segment_data)
            except Exception as e:
                logger.debug(f"Failed to download segment {i} from {segment_url}: {e}")

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


async def is_maybe_video_url(url: str, session: aiohttp.ClientSession) -> bool:
    """Returns True iff the URL points at an accessible video file/stream."""
    try:
        headers = await fetch_headers(url, session, timeout=3)
        content_type = headers.get('Content-Type') or headers.get('content-type')
        if content_type.startswith("video/") or content_type == "application/vnd.apple.mpegurl":
            # Surely a video
            return True
        else:
            # If the content is a binary download stream, it may encode a video
            # but also something else. This is a case of "maybe a video"
            return content_type == "binary/octet-stream"

    except Exception:
        return False


async def _ffmpeg_remux_hls_to_mp4(playlist_url: str) -> Optional[bytes]:
    """Use FFmpeg to read an HLS playlist (CMAF/fMP4) and remux to MP4, returning bytes.

    We pass headers for basic compatibility and copy streams without re-encoding.
    """
    import asyncio
    import shlex

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
        pass

    return None
