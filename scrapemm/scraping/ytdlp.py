import logging
import tempfile
import traceback
from datetime import datetime
from typing import Any, Optional

import asyncio
import aiohttp
from ezmm import MultimodalSequence, download_image, Video, Image
from yt_dlp import YoutubeDL

logger = logging.getLogger("scrapeMM")


async def _download_with_ytdlp(
        url: str,
        session: aiohttp.ClientSession
) -> tuple[Optional[Video], Optional[Image], Optional[dict[str, Any]]]:
    """Downloads a video, its thumbnail, and the metadata using yt-dlp."""
    try:
        with tempfile.NamedTemporaryFile() as temp_file:
            temp_path = temp_file.name

        ydl_opts: dict[str, Any] = dict(
            outtmpl=f'{temp_path}.%(ext)s',  # Output filename format
            format='best[ext=mp4]/best',  # Download the best video/audio quality
            quiet=False,  # Show progress in terminal
            noplaylist=True,  # Disable playlist downloading
            retries=3,
            cookies_from_browser='firefox',
            cookies='cookies.txt',  # Use cookies from Firefox to bypass sign-in requirements
        )

        if "youtube" in url or "youtu.be" in url:
            # YouTube delivers video and audio separately when downloaded above 720p.
            # This would require FFmpeg to merge them. Restrict to 720p to avoid that.
            ydl_opts['format'] = 'best[height<=720][acodec!=none]'
            ydl_opts['extractor_args'] = dict(youtube=dict(player_client=["default"]))

        with YoutubeDL(ydl_opts) as ydl:
            metadata = ydl.extract_info(url, download=True)

        ext = metadata.get("ext")

        try:
            video = Video(file_path=temp_path + f".{ext}", source_url=url)
            video.relocate(move_not_copy=True)
        except Exception as e:
            logger.warning(f"Could not load downloaded video: {e}\n{traceback.format_exc()}")
            video = None

        thumbnail = None
        if thumbnail_url := metadata.get('thumbnail'):
            thumbnail = await download_image(thumbnail_url, session)

        return video, thumbnail, metadata

    except Exception as e:
        logger.warning(f"Could not download video with yt-dlp: {e}\n{traceback.format_exc()}")
        return None, None, None


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
    if thumbnail:
        items.append(thumbnail)
    if video:
        items.append(video)

    return MultimodalSequence(items)


async def get_content_with_ytdlp(url: str, session: aiohttp.ClientSession, platform: str) -> MultimodalSequence | None:
    """Retrieves video, thumbnail, and metadata using the powerful yt-dlp package."""
    # Run the download in a separate thread to avoid blocking the event loop
    coroutine = await asyncio.to_thread(_download_with_ytdlp, url, session)
    video, thumbnail, metadata = await coroutine
    if not video:
        logger.warning(f"Video download failed for {url}")
    if metadata:
        return await compose_data_to_sequence(metadata, video, thumbnail, platform)
    return None
