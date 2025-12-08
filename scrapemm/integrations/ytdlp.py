import asyncio
import logging
import sys
import tempfile
import traceback
from datetime import datetime
from typing import Any, Optional

import aiohttp
from ezmm import MultimodalSequence, download_image, Video, Image
from yt_dlp import YoutubeDL

from scrapemm.common.exceptions import IPBannedError, ContentNotFoundError

logger = logging.getLogger("scrapeMM")

# Add yt-dlp-specific logger to print warnings to console
logger_yt_dlp = logging.getLogger("yt_dlp")
logger_yt_dlp.setLevel(logging.WARNING)
logger_yt_dlp.addHandler(logging.StreamHandler(sys.stdout))


async def download_video_with_ytdlp(
        url: str,
        session: aiohttp.ClientSession,
        max_video_size: int = None,
        cookie_file: str = None
) -> tuple[Optional[Video], Optional[Image], Optional[dict[str, Any]]]:
    """Downloads a video and (if not available or exceeds max. duration) its thumbnail, and the metadata using yt-dlp.
    @param max_video_size: Maximum video size in bytes. If the video is larger, the download will be aborted."""
    try:
        with tempfile.NamedTemporaryFile() as temp_file:
            temp_path = temp_file.name

        ydl_opts: dict[str, Any] = dict(
            outtmpl=f'{temp_path}.%(ext)s',  # Output filename format
            format='best[ext=mp4]/best',  # Download the best video/audio quality
            max_filesize=max_video_size,
            quiet=True,  # Silence logs in console
            logger=logger_yt_dlp,  # Reroute logs to dedicated logger
            noplaylist=True,  # Disable playlist downloading
            retries=3,
            cookiefile=cookie_file,  # Use cookie to bypass sign-in requirements
        )

        if "youtube" in url or "youtu.be" in url:
            # YouTube delivers video and audio separately when downloaded above 720p.
            # This would require FFmpeg to merge them. Restrict to 720p to avoid that.
            ydl_opts['format'] = 'best[height<=720]'
            ydl_opts['extractor_args'] = dict(youtube=dict(player_client=["default"]))

        with YoutubeDL(ydl_opts) as ydl:
            metadata = ydl.extract_info(url, download=True)

        video = None
        if ext := metadata.get("ext"):
            try:
                video = Video(file_path=temp_path + f".{ext}", source_url=url)
                video.relocate(move_not_copy=True)
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning(f"Could not load downloaded video: {e}")

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
        elif "Video unavailable" in str(e):
            raise ContentNotFoundError(f"Video is unavailable.")
        elif "Cannot parse data; please report this issue" in str(e):
            raise RuntimeError(f"yt-dlp is unable to parse the received video metadata.")
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
            pass

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
) -> MultimodalSequence | None:
    """Retrieves video, thumbnail, and metadata using the powerful yt-dlp package."""
    # Run the download in a separate thread to avoid blocking the event loop
    coroutine = await asyncio.to_thread(download_video_with_ytdlp, url, session, **kwargs)
    video, thumbnail, metadata = await coroutine
    if metadata:
        return await compose_data_to_sequence(metadata, video, thumbnail, platform)
    return None
