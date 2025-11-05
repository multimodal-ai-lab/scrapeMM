import sys
from typing import Any, cast

import aiohttp
import asyncio
from datetime import datetime
import json
import logging
import os
import subprocess
import tempfile

from ezmm import MultimodalSequence, download_image
from ezmm.common.items import Video, Image

logger = logging.getLogger("scrapeMM")


def check_ytdlp_available() -> bool:
    """Returns True if yt-dlp is available, else False."""
    try:
        # Run yt-dlp --version to check if it's installed and working'
        subprocess.run([sys.executable, '-m', 'yt_dlp', '--version'],
                     capture_output=True, check=True, timeout=5)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False


async def extract_metadata_with_ytdlp(url: str) -> dict[str, Any] | None:
    """Extracts metadata using yt-dlp without downloading the video."""
    try:
        cmd = [
            sys.executable,  # Ensure to use the same Python interpreter as the calling process
            '-m',
            'yt_dlp',
            '--no-download',
            '--print-json',
            '--no-warnings',
            '--quiet',
            url
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            logger.error(f"❌ yt-dlp metadata extraction failed: {stderr.decode()}")
            return None

        # Parse JSON output
        metadata = json.loads(stdout.decode())
        return metadata

    except Exception as e:
        logger.error(f"❌ Error extracting metadata with yt-dlp: {e}")
        return None
    

async def download_video_with_ytdlp(url: str) -> Video | None:
    """Downloads a video using yt-dlp."""
    try:
        with tempfile.NamedTemporaryFile(suffix='.%(ext)s', delete=False) as temp_file:
            temp_path = temp_file.name

        cmd = [
            sys.executable,  # Ensure to use the same Python interpreter as the calling process
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--no-warnings",
            "--quiet",
            "--retries", "10",
        ]

        if "youtube" in url or "youtu.be" in url:
            cmd.extend([
                "-f", "bv*[vcodec~='^avc1']+ba/bv*+ba/b",
                "--merge-output-format", "mp4",
            ])
        else:
            cmd.extend(['--format', 'best[ext=mp4]/best'])

        cmd.extend([
            "--output", temp_path,
            url
        ])

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        _, stderr = await process.communicate()
        
        if process.returncode != 0:
            logger.warning(f"yt-dlp video download failed: {stderr.decode()}")
            return None

        # Find the actual downloaded file (yt-dlp changes extension)
        downloaded_file = None
        for ext in ['.mp4', '.webm', '.mkv']:
            potential_file = temp_path.replace('.%(ext)s', ext)
            if os.path.exists(potential_file) and os.path.getsize(potential_file) > 0:
                downloaded_file = potential_file
                break

        if downloaded_file and os.path.exists(downloaded_file):
            with open(downloaded_file, 'rb') as f:
                video_data = f.read()
            
            os.unlink(downloaded_file)
            
            video = Video(binary_data=video_data, source_url=url)
            video.relocate(move_not_copy=True)
            return cast(Video, video)

        return None

    except Exception as e:
        logger.error(f"❌ Error downloading video with yt-dlp: {e}")
        return None
    

async def download_thumbnail_with_ytdlp(url: str, session: aiohttp.ClientSession) -> Image | None:
    """Downloads thumbnail using yt-dlp metadata."""
    try:
        metadata = await extract_metadata_with_ytdlp(url)
        if not metadata:
            return None

        thumbnail_url = metadata.get('thumbnail')
        if thumbnail_url:
            return await download_image(thumbnail_url, session)

    except Exception as e:
        logger.error(f"❌ Error downloading thumbnail: {e}")
    
    return None

def fmt_count(v):
    return f"{v:,}" if isinstance(v, int) else "Unknown"

async def create_video_sequence_from_ytdlp(metadata: dict, url: str, video: Video | None, thumbnail: Image | None, platform: str) -> MultimodalSequence:
    """Creates MultimodalSequence from yt-dlp metadata."""
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

async def get_video_with_ytdlp(url: str, session: aiohttp.ClientSession, platform: str) -> MultimodalSequence | None:
    """Retrieves video using only yt-dlp (no API required)."""
    try:
        # Get metadata and video using yt-dlp
        metadata = await extract_metadata_with_ytdlp(url)
        if not metadata:
            return None

        video = await download_video_with_ytdlp(url)
        thumbnail = await download_thumbnail_with_ytdlp(url, session)
        
        return await create_video_sequence_from_ytdlp(metadata, url, video, thumbnail, platform)

    except Exception as e:
        import traceback
        traceback.print_exc()
        logger.error(f"❌ Error retrieving video with yt-dlp: {e}")
        return None
