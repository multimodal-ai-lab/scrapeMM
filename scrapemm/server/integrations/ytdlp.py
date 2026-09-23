import asyncio
import logging
import sys
import tempfile
from datetime import datetime
from typing import Any, Optional

import aiohttp
from ezmm import MultimodalSequence, Video, Image
from yt_dlp import YoutubeDL

from scrapemm.common.exceptions import RetrievalFailed, TargetUnavailableError, AccessBlockedError, RateLimitError
from scrapemm.server.download import download_image

logger = logging.getLogger("scrapeMM")

# Cap on the video resolution. Keeps the downloads small; higher resolutions rarely add
# information that matters for our purposes.
MAX_VIDEO_HEIGHT = 720

# Add yt-dlp-specific logger to print warnings to console
logger_yt_dlp = logging.getLogger("yt_dlp")
logger_yt_dlp.setLevel(logging.CRITICAL)
logger_yt_dlp.addHandler(logging.StreamHandler(sys.stdout))


def _format_selector() -> str:
    """Builds the yt-dlp format selector.

    Neither YouTube nor Facebook serve progressive formats (video and audio muxed into a
    single file) any longer, so selectors like 'best[ext=mp4]' — which only ever match
    progressive formats — cannot resolve at all. Video and audio have to be downloaded
    separately and merged, which needs FFmpeg. Without FFmpeg, we take the video-only
    stream, i.e. the video comes without sound.
    """
    from scrapemm.server.environment import ffmpeg_available

    capped, uncapped = f"[height<={MAX_VIDEO_HEIGHT}]", ""
    if ffmpeg_available:
        # Prefer mp4+m4a, which merge into a clean mp4 without re-encoding
        return "/".join([f"bv*{capped}[ext=mp4]+ba[ext=m4a]", f"bv*{capped}+ba", f"b{capped}",
                         f"bv*{uncapped}+ba", "b"])
    return "/".join([f"b{capped}", "b", f"bv*{capped}[ext=mp4]", f"bv*{capped}", "bv*"])


def _run_ytdlp_sync(
        url: str,
        temp_path: str,
        ydl_opts: dict[str, Any],
) -> tuple[Optional[Video], Optional[dict[str, Any]]]:
    """Synchronous yt-dlp extraction. Runs blocking I/O and must be called
    via asyncio.to_thread so it does not stall the event loop."""
    with YoutubeDL(ydl_opts) as ydl:
        metadata = ydl.extract_info(url, download=True)

    video = None
    if ext := metadata.get("ext"):
        try:
            video = Video(file_path=temp_path + f".{ext}", source_url=url)
            video.relocate(move_not_copy=True)
        except FileNotFoundError:
            logger.debug(f"yt-dlp reported ext={ext} but file was not found at {temp_path}.{ext}")
        except Exception as e:
            logger.warning(f"Could not load downloaded video: {e}")

    return video, metadata


async def download_video_with_ytdlp(
        url: str,
        session: aiohttp.ClientSession,
        max_video_size: int | None = None,
        **kwargs
) -> tuple[Optional[Video], Optional[Image], Optional[dict[str, Any]]]:
    """Downloads a video and (if not available or exceeds max. duration) its thumbnail, and the metadata using yt-dlp.
    @param max_video_size: Maximum video size in bytes. If the video is larger, the download will be aborted."""
    try:
        # Remove scrapeMM-specific kwargs which yt-dlp does not understand
        for key in ("format", "output_format", "include_media"):
            kwargs.pop(key, None)

        with tempfile.NamedTemporaryFile() as temp_file:
            temp_path = temp_file.name

        ydl_opts: dict[str, Any] = dict(
            outtmpl=f'{temp_path}.%(ext)s',  # Output filename format
            format=_format_selector(),
            merge_output_format="mp4",  # Keep the container predictable when merging
            max_filesize=max_video_size,
            quiet=True,  # Silence logs in console
            logger=logger_yt_dlp,  # Reroute logs to dedicated logger
            noplaylist=True,  # Disable playlist downloading
            retries=3,
            ignoreerrors=False,
            **kwargs
        )

        # Tell yt-dlp where FFmpeg is. It only searches PATH on its own, so a binary
        # found via FFMPEG_PATH or an imageio-ffmpeg install would stay invisible to
        # it -- and yt-dlp would silently skip merging the video and audio streams.
        from scrapemm.server.download.videos import _resolve_ffmpeg_path
        if ffmpeg := _resolve_ffmpeg_path():
            ydl_opts["ffmpeg_location"] = ffmpeg

        if "youtube" in url or "youtu.be" in url:
            ydl_opts['extractor_args'] = dict(youtube=dict(player_client=["default"]))

        # Run blocking yt-dlp work in a thread pool to avoid stalling the event loop.
        video, metadata = await asyncio.to_thread(_run_ytdlp_sync, url, temp_path, ydl_opts)

        if video and metadata.get("acodec") in (None, "none"):
            logger.info(f"⚠️ Downloaded {video.reference} without audio. Install FFmpeg to "
                        f"enable merging the separate video and audio streams.")

        if video and max_video_size and video.size > max_video_size:
            logger.info(f"Removing video {video.reference} because it exceeds the maximum size "
                        f"of {max_video_size / 1024 / 1024:.2f} MB.")
            video = None  # Discard video

        thumbnail = None
        if not video:
            thumbnail_url = metadata.get('thumbnail')
            if thumbnail_url:
                thumbnail = await download_image(thumbnail_url, session)

        return video, thumbnail, metadata

    except Exception as e:
        if "The following content is not available on this app" in str(e):
            logger.warning(f"You should update yt-dlp to re-enable YouTube downloads.")
            raise e
        elif ("Video unavailable" in str(e)
              or "HTTP Error 404: Not Found" in str(e)
              or "Instagram sent an empty media response" in str(e)):
            raise TargetUnavailableError(f"Target content not found (Error 404).")
        elif "Cannot parse data; please report this issue" in str(e):
            raise RetrievalFailed(f"yt-dlp is unable to parse the target content.")
        elif "There is no video in this post" in str(e) or "No video formats found" in str(e):
            raise RetrievalFailed(f"Target content has no video.")
        elif "Error 403: Forbidden" in str(e):
            raise AccessBlockedError(f"Access to target content forbidden.")
        elif "Sign in to confirm you’re not a bot" in str(e):
            raise AccessBlockedError(f"Login required to access target content.")
        elif "This video has been removed" in str(e):
            raise AccessBlockedError(f"Target content has been removed.")
        elif "This content isn't available to everyone" in str(e):
            raise AccessBlockedError(f"Target content not available to everyone.")
        elif "rate-limit reached" in str(e):
            raise RateLimitError(f"Rate limit reached: {e}")
        else:
            raise RuntimeError(f"Could not download video with yt-dlp: {e}")


def fmt_count(v):
    return f"{v:,}" if isinstance(v, int) else "Unknown"


async def compose_data_to_sequence(metadata: dict, video: Video | None, thumbnail: Image | None,
                                   platform: str) -> MultimodalSequence:
    """Creates a MultimodalSequence from the yt-dlp metadata."""
    # title = metadata.get('title', '')
    uploader = metadata.get('uploader', 'Unknown')
    upload_date = metadata.get('upload_date', '')
    duration = metadata.get('duration', 0)
    view_count = metadata.get('view_count', 0)
    like_count = metadata.get('like_count', 0)
    comment_count = metadata.get('comment_count', 0)
    description = metadata.get('description', '')

    # Format upload date
    formatted_date = upload_date
    if upload_date and len(upload_date) == 8:
        try:
            date_obj = datetime.strptime(upload_date, '%Y%m%d')
            formatted_date = date_obj.strftime('%Y-%m-%d')
        except ValueError:
            logger.debug(f"Could not parse yt-dlp upload_date: {upload_date!r}")

    text = f"""**{platform} Video**
Author: @{uploader}
Posted: {formatted_date}
Duration: {duration}s
Views: {fmt_count(view_count)} - Likes: {fmt_count(like_count)} - Comments: {fmt_count(comment_count)}

{description}"""

    items: list = [text]
    if video:
        items.append(video)
    elif thumbnail:  # Only add thumbnail if video could not be downloaded
        items.append(thumbnail)

    return MultimodalSequence(items)


async def get_content_with_ytdlp(
        url: str,
        session: aiohttp.ClientSession,
        platform: str,
        **kwargs
) -> MultimodalSequence:
    """Retrieves video, thumbnail, and metadata using the powerful yt-dlp package."""
    video, thumbnail, metadata = await download_video_with_ytdlp(url, session, **kwargs)
    if metadata:
        return await compose_data_to_sequence(metadata, video, thumbnail, platform)
    else:
        raise RetrievalFailed("Could not retrieve video metadata.")
