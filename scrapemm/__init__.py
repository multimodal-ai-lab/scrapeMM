from .common import APP_NAME, set_wait_on_rate_limit, RateLimitError, ContentNotFoundError, logger, update_config
from .integrations import Telegram, X
from .retrieval import retrieve
from .secrets import configure_secrets
from .util import run_command

# Check if ffmpeg is available.
try:
    run_command(["ffmpeg", "-version"])
    ffmpeg_available = True
except FileNotFoundError:
    logger.warning("⚠️ FFmpeg not found. Won't normalize videos. If you want to enable it, please install FFmpeg "
                   "via `conda install -c conda-forge ffmpeg`.")
    ffmpeg_available = False
