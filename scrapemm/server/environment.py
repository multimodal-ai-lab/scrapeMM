"""What the machine running the server can actually do.

The answers are cheap but not free (they shell out), so they are computed once and
reported to the dashboard rather than re-derived at every retrieval.
"""

import logging
import shutil

from scrapemm.common.paths import APP_NAME
from .download.videos import _resolve_ffmpeg_path

logger = logging.getLogger(APP_NAME)

# FFmpeg merges the separate video and audio streams that YouTube and Facebook serve,
# and normalizes videos into a format browsers can play. Uses the same resolver as the
# code that runs FFmpeg, so a bare `ffmpeg` missing from PATH does not disable features
# that would work via an explicit FFMPEG_PATH or an imageio-ffmpeg install.
ffmpeg_path = _resolve_ffmpeg_path()
ffmpeg_available = ffmpeg_path is not None

# Normalizing additionally needs ffprobe, which a full FFmpeg install ships but the
# imageio-ffmpeg package does not.
ffprobe_available = shutil.which("ffprobe") is not None

if not ffmpeg_available:
    logger.warning("⚠️ FFmpeg not found. Won't normalize videos. If you want to enable it, "
                   "please install FFmpeg via `conda install -c conda-forge ffmpeg`.")
