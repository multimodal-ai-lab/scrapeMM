import asyncio
import logging
import os
import random
import re
import sys
import tempfile
import time
from datetime import datetime
from typing import Any, Optional

import aiohttp
from ezmm import MultimodalSequence, Video, Image
from yt_dlp import YoutubeDL
from yt_dlp.utils import (DownloadError, ExtractorError, GeoRestrictedError,
                          UnsupportedError, UserNotLive, YoutubeDLError)

from scrapemm.common.exceptions import RetrievalFailed, TargetUnavailableError, AccessBlockedError, RateLimitError
from scrapemm.server.config import get_config_var
from scrapemm.server.download import download_image

logger = logging.getLogger("scrapeMM")

# Cap on the video resolution. Keeps the downloads small; higher resolutions rarely add
# information that matters for our purposes.
MAX_VIDEO_HEIGHT = 720

# Add yt-dlp-specific logger to print warnings to console
logger_yt_dlp = logging.getLogger("yt_dlp")
logger_yt_dlp.setLevel(logging.CRITICAL)
logger_yt_dlp.addHandler(logging.StreamHandler(sys.stdout))

# yt-dlp loads its plugins (here: the bgutil PO token providers) lazily, when the first
# YoutubeDL is created -- guarded by a plain flag, not a lock. The server creates them in
# worker threads, several at once, so the first batch after a start imported the plugins
# concurrently: half-initialized modules, providers registered twice, and the PO token
# provider missing until the next restart. Loading them once here, at import in the main
# thread, leaves nothing for those threads to do.
from yt_dlp.globals import all_plugins_loaded as _all_plugins_loaded
from yt_dlp.plugins import load_all_plugins as _load_all_plugins
if not _all_plugins_loaded.value:
    _load_all_plugins()


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


def _youtube_format_selector() -> str:
    """YouTube's HLS (m3u8) formats first. Its direct (https/DASH) format URLs demand a
    "GVS" PO token for most player clients: without one, the download is answered with
    HTTP 403 -- always on the production server, one time in three elsewhere -- while
    the HLS formats need none. H.264 first among them, which browsers play as it is.
    Without FFmpeg, nothing can merge HLS video with separate audio, so the general
    selector applies."""
    from scrapemm.server.environment import ffmpeg_available
    if not ffmpeg_available:
        return _format_selector()
    capped = f"[height<={MAX_VIDEO_HEIGHT}]"
    return "/".join([f"bv*{capped}[protocol^=m3u8][vcodec^=avc1]+ba[ext=m4a]",
                     f"bv*{capped}[protocol^=m3u8]+ba", _format_selector()])


# Only HLS: the second attempt after a direct format was refused (HTTP 403)
_YOUTUBE_HLS_ONLY = (f"bv*[height<={MAX_VIDEO_HEIGHT}][protocol^=m3u8]+ba[protocol^=m3u8]/"
                     f"bv*[protocol^=m3u8]+ba[protocol^=m3u8]/b[protocol^=m3u8]")

# Where the bgutil PO token provider's script lives (see the Dockerfile). yt-dlp's
# plugin runs it with Deno to mint the PO tokens that the fallback player client needs.
BGUTIL_SERVER_HOME = os.environ.get("BGUTIL_SERVER_HOME",
                                    "/opt/bgutil-ytdlp-pot-provider/server")


# --- YouTube's bot check ------------------------------------------------------------
#
# After a burst of requests from one IP address, YouTube answers every further one with
# "Sign in to confirm you're not a bot" -- and keeps doing so for a while. Nothing short
# of a different address reliably gets past it, so the aim is not to trigger it: all
# YouTube retrievals on this server start at a steady pace (yt-dlp's documented guest
# limit is about 300 videos an hour), and once the check does come up, YouTube is left
# alone for a while rather than asked again, which only extends the flag.
#
# The check is issued per player client, though: when the default clients meet it, the
# mobile web client ("mweb", with a PO token minted locally) often still gets through.
# So each group of clients is paused on its own, and YouTube as a whole only when all are.

# Player clients, in the order they are tried: yt-dlp's default set, then mweb, whose
# formats need a PO token (see BGUTIL_SERVER_HOME) and which answers when the default
# clients are asked to prove they are no bot.
YOUTUBE_CLIENT_GROUPS: tuple[tuple[str, ...], ...] = (("default",), ("mweb",))

# Seconds between the starts of two YouTube retrievals, with up to +50% jitter
DEFAULT_YOUTUBE_MIN_INTERVAL = 12.0
# Seconds YouTube is not asked again after it demanded the bot check
DEFAULT_YOUTUBE_COOLDOWN = 30 * 60.0


class _YouTubeGate:
    """The one pace all YouTube retrievals of this process keep, and the pauses after a
    bot check, per group of player clients. Server-wide, because yt-dlp's own sleep
    options only space the requests of one call, while the server runs many at once."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._next_start = 0.0
        self._blocked_until: dict[tuple[str, ...], float] = {}
        self.reason = ""

    @property
    def blocked_until(self) -> float:
        """Until when YouTube as a whole is paused (0 if some client group is open)."""
        return min((self._blocked_until.get(g, 0.0) for g in YOUTUBE_CLIENT_GROUPS), default=0.0)

    def open_groups(self) -> list[tuple[str, ...]]:
        """The client groups not paused, in order. Fails at once if none is open."""
        now = time.time()
        groups = [g for g in YOUTUBE_CLIENT_GROUPS if self._blocked_until.get(g, 0.0) <= now]
        if not groups:
            until = datetime.fromtimestamp(self.blocked_until).strftime("%H:%M")
            raise RateLimitError(
                f"YouTube demanded its bot check ({self.reason}), so YouTube retrievals are "
                f"paused until {until} to let the flag on this server's address expire. "
                f"Asking again now would only extend it.")
        return groups

    def check(self) -> None:
        """Fails at once while YouTube is being left alone."""
        self.open_groups()

    async def wait_turn(self) -> None:
        self.check()
        interval = float(get_config_var("youtube_min_interval", DEFAULT_YOUTUBE_MIN_INTERVAL))
        async with self._lock:
            delay = self._next_start - time.time()
            if delay > 0:
                logger.debug(f"Pacing YouTube: waiting {delay:.1f}s for the next slot.")
                await asyncio.sleep(delay)
            self._next_start = time.time() + interval * random.uniform(1.0, 1.5)

    def trip(self, reason: str, group: Optional[tuple[str, ...]] = None) -> None:
        """Pauses one client group (all of them if None) after it met the bot check."""
        cooldown = float(get_config_var("youtube_cooldown", DEFAULT_YOUTUBE_COOLDOWN))
        now = time.time()
        span = f"{cooldown / 60:.0f} min" if cooldown >= 60 else f"{cooldown:.0f} s"
        for g in ([group] if group else YOUTUBE_CLIENT_GROUPS):
            if self._blocked_until.get(g, 0.0) > now:
                continue  # Already paused; one warning is enough
            self._blocked_until[g] = now + cooldown
            self.reason = reason
            if self.blocked_until > now:
                logger.warning(f"🤖 YouTube demanded its bot check from every player client. "
                               f"Pausing YouTube retrievals for {span} so the flag on this "
                               f"address can expire.")
            else:
                logger.info(f"🤖 YouTube demanded its bot check from the {'/'.join(g)} "
                            f"client(s); using the others for {span}.")


youtube_gate = _YouTubeGate()


def _is_youtube(url: str) -> bool:
    return any(host in url for host in ("youtube.com", "youtu.be", "youtube-nocookie.com"))


def _is_bot_wall(error: Exception) -> bool:
    """Whether YouTube refused because it takes this server for a bot (or is rate
    limiting it), as opposed to this one video being unavailable."""
    return isinstance(error, RateLimitError) or (
        isinstance(error, AccessBlockedError) and "no bot" in str(error))


class NotASingleVideo(RetrievalFailed):
    """The URL is a channel, playlist or profile rather than one video."""


# What yt-dlp's error messages mean, checked in order against the lower-cased message.
# First match wins, so the more specific phrases come first.
_ERROR_PATTERNS: list[tuple[tuple[str, ...], type[Exception], str]] = [
    (("the following content is not available on this app",),
     RetrievalFailed, "yt-dlp is outdated for this site; update it"),
    (("rate-limit", "rate limit", "too many requests", "http error 429"),
     RateLimitError, "Rate limit reached"),
    # Before the bot check: an age gate also says "Sign in to confirm (your age)", and
    # taking it for the bot check paused all of YouTube for half an hour
    (("confirm your age", "age-restricted", "age restricted", "inappropriate for some users"),
     AccessBlockedError, "Age-restricted"),
    (("not a bot", "confirm you're not", "confirm you’re not", "confirm that you're not"),
     AccessBlockedError, "The platform demands a login to prove this is no bot"),
    (("private video", "this video is private", "members-only", "join this channel",
      "login required", "log in", "sign in", "requires authentication"),
     AccessBlockedError, "Only accessible when logged in or subscribed"),
    (("not available in your country", "geo restrict", "geo-restrict",
      "not available from your location", "blocked it in your country"),
     AccessBlockedError, "Not available in the server's region"),
    (("copyright", "not available to everyone", "http error 403", "forbidden"),
     AccessBlockedError, "Access forbidden"),
    # Before the generic "is not available" below, which would swallow these
    (("no video formats", "there is no video", "requested format is not available",
      "no video in this post"),
     RetrievalFailed, "The target has no downloadable video"),
    (("live event will begin", "premieres in", "is not currently live", "is offline"),
     RetrievalFailed, "The video has not started yet"),
    (("has been removed", "been deleted", "account has been terminated",
      "account associated with this video has been terminated", "no longer available"),
     TargetUnavailableError, "The content has been removed"),
    # Not a bare "not found": that would also match a missing ffmpeg
    (("video unavailable", "is not available", "does not exist", "http error 404",
      "404: not found", "empty media response"),
     TargetUnavailableError, "The content is not available"),
    (("cannot parse data", "unable to extract", "unsupported url"),
     RetrievalFailed, "yt-dlp cannot extract this page"),
    (("timed out", "connection reset", "connection refused", "name resolution",
      "network is unreachable", "http error 5", "remote end closed"),
     TargetUnavailableError, "The platform could not be reached"),
]

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _classify(error: Exception) -> Exception:
    """Turns a yt-dlp failure into the scrapeMM exception that says what happened, so
    the engine can report it plainly and move on to the next method. Only failures that
    are neither recognised nor declared expected by yt-dlp remain a RuntimeError: those
    are the ones worth a traceback."""
    # DownloadError is only the envelope yt-dlp reports errors in; the cause is inside
    cause = error
    if isinstance(error, DownloadError) and error.exc_info and error.exc_info[1]:
        cause = error.exc_info[1]
    message = _ANSI.sub("", str(getattr(cause, "orig_msg", None) or cause)).removeprefix("ERROR: ")

    if isinstance(cause, GeoRestrictedError):
        return AccessBlockedError(f"Not available in the server's region: {message}")
    if isinstance(cause, UnsupportedError):
        return RetrievalFailed(f"yt-dlp does not support this URL: {message}")
    if isinstance(cause, UserNotLive):
        return RetrievalFailed(f"The stream is not live: {message}")

    lowered = message.lower()
    for phrases, exception_type, summary in _ERROR_PATTERNS:
        if any(phrase in lowered for phrase in phrases):
            if exception_type is RetrievalFailed and "update it" in summary:
                logger.warning("yt-dlp needs an update to download from this site again.")
            return exception_type(f"{summary}: {message}")

    if isinstance(cause, ExtractorError) and cause.expected:
        # yt-dlp knows this failure and blames the content, not itself
        return RetrievalFailed(message)
    return RuntimeError(f"Could not download video with yt-dlp: {message}")


def _run_ytdlp_sync(
        url: str,
        temp_path: str,
        ydl_opts: dict[str, Any],
) -> tuple[Optional[Video], Optional[dict[str, Any]]]:
    """Synchronous yt-dlp extraction. Runs blocking I/O and must be called
    via asyncio.to_thread so it does not stall the event loop."""
    with YoutubeDL(ydl_opts) as ydl:
        metadata = ydl.extract_info(url, download=True)

    if metadata and metadata.get("_type") in ("playlist", "multi_video"):
        raise NotASingleVideo(f"{url} is a channel or playlist, not a single video.")

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
            # noplaylist only covers a video inside a playlist. A channel or playlist URL
            # would otherwise have its entries downloaded one after another; this way
            # they are merely listed, and the URL is recognised as no single video.
            extract_flat="in_playlist",
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

        if _is_youtube(url):
            video, metadata = await _run_youtube(url, temp_path, ydl_opts)
        else:
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

    except (NotASingleVideo, RateLimitError, AccessBlockedError, RetrievalFailed,
            TargetUnavailableError):
        raise  # Already classified (the YouTube path classifies its own errors)
    except YoutubeDLError as e:
        raise _classify(e) from e
    except Exception as e:
        raise RuntimeError(f"Could not download video with yt-dlp: {e}") from e


async def _run_youtube(url: str, temp_path: str,
                       ydl_opts: dict[str, Any]) -> tuple[Optional[Video], Optional[dict[str, Any]]]:
    """Downloads from YouTube with the first group of player clients not paused for the
    bot check, falling back to the next group when a group meets it. A direct format
    refused with HTTP 403 is retried once as HLS."""
    ydl_opts = dict(ydl_opts, format=_youtube_format_selector(),
                    # IPv4 only: YouTube judges an IPv6 address together with its whole
                    # /64, and more harshly
                    source_address="0.0.0.0")
    last_error: Optional[Exception] = None
    for group in youtube_gate.open_groups():
        extractor_args: dict[str, Any] = dict(youtube=dict(player_client=list(group)))
        if os.path.isdir(BGUTIL_SERVER_HOME):
            extractor_args["youtubepot-bgutilscript"] = dict(server_home=[BGUTIL_SERVER_HOME])
        opts = dict(ydl_opts, extractor_args=extractor_args)
        for hls_only in (False, True):
            if hls_only:
                opts = dict(opts, format=_YOUTUBE_HLS_ONLY)
            await youtube_gate.wait_turn()
            try:
                return await asyncio.to_thread(_run_ytdlp_sync, url, temp_path, opts)
            except YoutubeDLError as e:
                last_error = classified = _classify(e)
                if _is_bot_wall(classified):
                    youtube_gate.trip(str(classified).split(":")[0], group)
                    break  # On to the next client group
                if not hls_only and "http error 403" in str(classified).lower():
                    logger.info(f"YouTube refused a direct format of {url} (HTTP 403); "
                                f"retrying with HLS only.")
                    continue
                raise classified from e
    raise last_error or RateLimitError("YouTube retrievals are paused.")


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
