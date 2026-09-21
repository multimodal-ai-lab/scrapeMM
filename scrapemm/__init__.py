from .common import (APP_NAME, set_wait_on_rate_limit, RateLimitError, RetrievalFailed, logger,
                     update_config, ScrapingResponse, ScrapedContent, set_cache_ttl, clear_cache,
                     CaptchaEncounteredError, blacklist_domain, unblacklist_domain,
                     get_blacklisted_domains)
from .integrations import Telegram, X
from .integrations.archive_today import configure_archive_today_session
from .retrieval import retrieve
from .secrets import configure_secrets, override_secret, set_secret

# Check if ffmpeg is available. Uses the same resolver as the code that runs FFmpeg,
# so a bare `ffmpeg` missing from PATH does not disable features that would work via
# an explicit FFMPEG_PATH or an imageio-ffmpeg install.
from .download.videos import _resolve_ffmpeg_path

ffmpeg_available = _resolve_ffmpeg_path() is not None
if not ffmpeg_available:
    logger.warning("⚠️ FFmpeg not found. Won't normalize videos. If you want to enable it, please install FFmpeg "
                   "via `conda install -c conda-forge ffmpeg`.")
