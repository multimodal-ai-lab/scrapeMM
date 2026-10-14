import asyncio
import copy
import glob
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit

import aiohttp
from ezmm import MultimodalSequence, Video, Image
from yt_dlp import YoutubeDL
from yt_dlp.utils import (DownloadError, ExtractorError, GeoRestrictedError,
                          UnsupportedError, UserNotLive, YoutubeDLError)

from scrapemm.common.exceptions import RetrievalFailed, TargetUnavailableError, AccessBlockedError, RateLimitError
from scrapemm.server import proxy, slots
from scrapemm.server.config import get_config_var, get_setting
from scrapemm.server.download import download_image
from scrapemm.server.download.videos import downscaling, is_too_small_video

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
# alone for a while rather than asked again, which only extends the flag. The pace is
# the cooldown of the "youtube" slot (see `scrapemm.server.slots`), which every YouTube
# download takes: the YouTube integration's, and those of YouTube videos embedded in
# other pages. One slot covers a download's attempts, the HLS retry included.
#
# Once YouTube refuses this server -- the bot check, or a refused stream (HTTP 403,
# fragments answered 404), which it serves an address it distrusts -- all of YouTube is
# paused at once, and every YouTube URL fails in an instant with the pause's message until
# it is over. Trying further player clients first cost minutes per URL (production,
# 2026-10: 113-155 s each, and the mobile client was refused just the same).
#
# While YouTube is paused and a proxy is in use (Settings, Proxy), YouTube retrievals go
# through the proxy instead, neither paced nor pausing anything: the flag is on this
# server's address, not the proxy's. Through the proxy, each group of player clients is
# tried in turn.

# Player clients, in the order they are tried through the proxy: yt-dlp's default set,
# then mweb, whose formats need a PO token (see BGUTIL_SERVER_HOME) and which sometimes
# answers when the default clients are asked to prove they are no bot. Directly, only the
# first group.
YOUTUBE_CLIENT_GROUPS: tuple[tuple[str, ...], ...] = (("default",), ("mweb",))

YOUTUBE_SLOT = "youtube"
YOUTUBE = "YouTube"  # The method's name, as the proxies' records know it

# Seconds YouTube is not asked again after it refused this server (bot check, stream);
# the setting `youtube_pause`, formerly `youtube_cooldown`
DEFAULT_YOUTUBE_PAUSE = 30 * 60.0


def youtube_pause() -> float:
    configured = get_config_var("youtube_pause")
    if configured is None:
        configured = get_config_var("youtube_cooldown")  # A config from before
    return DEFAULT_YOUTUBE_PAUSE if configured is None else float(configured)


class _YouTubeGate:
    """The pause after YouTube refused this server: server-wide, as the flag is on the
    server's address."""

    def __init__(self):
        self.blocked_until = 0.0  # Until when YouTube is paused (0: not paused)
        self.reason = ""

    def paused(self) -> bool:
        return self.blocked_until > time.time()

    def hopeless(self) -> bool:
        """Whether a YouTube retrieval would fail now whatever it tried: YouTube is paused
        for this server's address, and there is no proxy to go through -- none configured,
        or each resting after YouTube refused it too (see `proxy.learn()`)."""
        return self.paused() and not proxy.available(YOUTUBE)

    def fail_fast(self) -> None:
        """Fails at once if a retrieval would be hopeless (see `hopeless()`): asked by the
        "youtube" slot before a URL waits for it, so that while YouTube refuses everything,
        its URLs fail in an instant instead of queueing for slots and cooldowns -- with
        one slot, a test run's six YouTube URLs waited up to 33 s each, only to fail."""
        if self.hopeless():
            self.check()

    def check(self) -> None:
        """Fails at once while YouTube is being left alone."""
        if self.paused():
            until = datetime.fromtimestamp(self.blocked_until).strftime("%H:%M")
            if proxy.available(YOUTUBE):
                meanwhile = "YouTube videos go through a proxy meanwhile."
            elif any(p.enabled for p in proxy.configured()):
                meanwhile = "YouTube refused every proxy in use as well, or none works."
            else:
                meanwhile = "A proxy (Settings, Proxy) would retrieve YouTube videos meanwhile."
            raise RateLimitError(
                f"YouTube refused this server ({self.reason}), so YouTube retrievals are "
                f"paused until {until} to let the flag on this server's address expire. "
                f"Asking again now would only extend it. {meanwhile}")

    def trip(self, reason: str) -> None:
        """Pauses YouTube after it refused this server."""
        if self.paused():
            return  # Already paused (by a URL in flight at the same time); one warning is enough
        pause = youtube_pause()
        span = f"{pause / 60:.0f} min" if pause >= 60 else f"{pause:.0f} s"
        self.blocked_until, self.reason = time.time() + pause, reason
        # The URLs waiting out the slot's cooldown: paused, they need not (see below)
        slots.wake(YOUTUBE_SLOT)
        logger.warning(f"🤖 YouTube refused this server ({reason}). Pausing YouTube "
                       f"retrievals for {span} so the flag on this address can expire"
                       f"{'; going through a proxy meanwhile' if proxy.available(YOUTUBE) else ''}.")


youtube_gate = _YouTubeGate()


def _proxy_refused(method: str) -> None:
    """After YouTube refused a proxy too (it then rests from YouTube; see `proxy.learn()`):
    if none is left, the URLs waiting for the slot fail at once."""
    if method == YOUTUBE and youtube_gate.hopeless():
        slots.wake(YOUTUBE_SLOT)


proxy.on_refusal(_proxy_refused)

def _through_a_proxy_now() -> bool:
    """Whether a YouTube retrieval about to start skips the pace of this server's address
    and goes through a proxy instead (see `_download_youtube()`): routed through one by
    the engine already; YouTube paused for this server; or this server's lane busy or
    cooling down while a proxy is idle."""
    if proxy.routed() or youtube_gate.paused():
        return True
    gate = slots.gate(YOUTUBE_SLOT)
    direct_busy = gate.in_use >= slots.limit_of(YOUTUBE_SLOT) or gate.next_start > time.time()
    return direct_busy and proxy.idle(YOUTUBE)


# No pace through a proxy: YouTube flags this server's address, not the proxies'. So
# with proxies, YouTube runs one retrieval per usable proxy besides its own slots, and
# a start that would wait for this server's pace goes through an idle proxy instead.
slots.exempt_from_cooldown(YOUTUBE_SLOT, _through_a_proxy_now)
slots.extra_slots(YOUTUBE_SLOT, lambda: proxy.usable_count(YOUTUBE))
# ...and no waiting at all when it would fail anyway
slots.fail_fast(YOUTUBE_SLOT, youtube_gate.fail_fast)


def _is_youtube(url: str) -> bool:
    return any(host in url for host in ("youtube.com", "youtu.be", "youtube-nocookie.com"))


def _is_bot_wall(error: Exception) -> bool:
    """Whether YouTube refused because it takes this server for a bot (or is rate
    limiting it), as opposed to this one video being unavailable."""
    return isinstance(error, RateLimitError) or (
        isinstance(error, AccessBlockedError) and "no bot" in str(error))


class NotASingleVideo(RetrievalFailed):
    """The URL is a channel, playlist or profile rather than one video."""


class YtDlpFailed(RuntimeError):
    """A yt-dlp failure that is not recognised (see `_classify()`): scrapeMM's to fix."""


STREAM_REFUSED = "The platform refused to serve the video's stream to this server"


def _stream_refused(error: Exception) -> bool:
    """Whether the video was found but its stream was refused: HTTP 403, or fragments
    answered 404 so that the download came out empty. YouTube does both to an address
    it distrusts (e.g. production, 2026-10, on every HLS fragment of its default client),
    while the same video downloads fine from elsewhere."""
    return isinstance(error, AccessBlockedError) and (
        str(error).startswith(STREAM_REFUSED) or "http error 403" in str(error).lower())


# What yt-dlp's error messages mean, checked in order against the lower-cased message.
# First match wins, so the more specific phrases come first.
_ERROR_PATTERNS: list[tuple[tuple[str, ...], type[Exception], str]] = [
    (("the following content is not available on this app",),
     RetrievalFailed, "yt-dlp is outdated for this site; update it"),
    (("rate-limit", "rate limit", "too many requests", "http error 429"),
     RateLimitError, "Rate limit reached"),
    # Found, but every fragment answered 404 (or the file came out empty otherwise)
    # (with fragments failing fast, the first one's 404 itself: "[download] Got error: HTTP
    # Error 404" -- the video was found, so it is no missing video)
    (("downloaded file is empty", "fragment not found", "not found, unable to continue",
      "[download] got error: http error 404"),
     AccessBlockedError, STREAM_REFUSED),
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
    # Instagram: "This content isn't available to everyone: It can't be seen by certain
    # audiences." (age- or region-restricted, shown to logged-in, eligible users only)
    (("isn't available to everyone", "certain audiences"),
     AccessBlockedError, "Restricted to certain audiences (age or region)"),
    (("copyright","not available to everyone", "http error 403", "forbidden"),
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
    (("cannot parse data", "unable to extract", "unsupported url", "no suitable extractor"),
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
    return YtDlpFailed(f"Could not download video with yt-dlp: {message}")


# The metadata key under which a download says why it left the video out, for the
# response to say so (see `compose_data_to_sequence()`)
VIDEO_OMITTED = "scrapemm_video_omitted"


class _TooLarge(Exception):
    """Raised by the progress hook: the download outgrew the size limit."""


def _codec(fmt: dict) -> str:
    return (fmt.get("vcodec") or "none").split(".")[0].replace("vp09", "vp9")


def _format_size(fmt: dict, formats: list[dict], duration: Optional[float] = None
                 ) -> Optional[int]:
    """A format's size in bytes, as far as known. HLS formats state none, but a direct
    format of the same resolution and codec does: YouTube's are the same stream, only
    delivered otherwise. With a `duration`, the bitrate estimates the rest, which
    rather overstates it (an HLS bitrate is the peak)."""
    if size := fmt.get("filesize") or fmt.get("filesize_approx"):
        return size
    if fmt.get("height") and _codec(fmt) != "none":
        twins = [f.get("filesize") or f.get("filesize_approx") for f in formats
                 if f.get("height") == fmt["height"] and _codec(f) == _codec(fmt)]
        if twins := [size for size in twins if size]:
            return max(twins)
    if duration and (tbr := fmt.get("tbr")):
        return int(tbr * 125 * duration)  # kbit/s to bytes
    return None


def _selection_size(info: dict, estimate: bool = False) -> Optional[int]:
    """The size of the format(s) yt-dlp selected, video and audio together; None if
    any part's is unknown. `estimate` lets the bitrate fill in (see `_format_size()`)."""
    formats = info.get("formats") or []
    duration = info.get("duration") if estimate else None
    sizes = [_format_size(f, formats, duration) for f in info.get("requested_formats") or [info]]
    return None if None in sizes else sum(sizes)


def _mb(size: float) -> str:
    return f"{size / 1024 / 1024:.0f} MB"


# The share of the size limit a downscaled video may be estimated at. An HLS stream comes
# larger than its direct twin, by which it is estimated: YouTube's 360p by more than 8%
# (plzE2jioaNU: estimated 95 MB, stopped at 102 MB), its 720p hardly at all.
FIT_MARGIN = 0.85


def _fitting_selector(info: dict, ydl_opts: dict[str, Any], limit: int) -> Optional[str]:
    """The format selector for the highest resolution below the selected one whose
    download fits into `limit` bytes, with a margin (see `FIT_MARGIN`); None if none does."""
    formats = info.get("formats") or []
    heights = sorted({f["height"] for f in formats if f.get("height") and _codec(f) != "none"
                      and f["height"] < (info.get("height") or MAX_VIDEO_HEIGHT + 1)},
                     reverse=True)
    for height in heights:
        selector = ydl_opts["format"].replace(f"height<={MAX_VIDEO_HEIGHT}", f"height<={height}")
        with YoutubeDL(dict(ydl_opts, format=selector)) as ydl:
            picked = ydl.process_ie_result(copy.deepcopy(info), download=False)
        if (size := _selection_size(picked, estimate=True)) and size <= limit * FIT_MARGIN:
            logger.info(f"Downscaling {info.get('webpage_url', 'the video')} to {height}p "
                        f"(about {_mb(size)}) to fit into the size limit of {_mb(limit)}.")
            return selector
    return None


def _size_guard(limit: int):
    """A progress hook that stops the download once it has outgrown `limit` bytes, all
    its parts (video, audio) together. yt-dlp's own `max_filesize` only acts on sizes
    known in advance, which HLS formats lack."""
    downloaded: dict[str, int] = {}

    def hook(progress: dict) -> None:
        if progress.get("status") == "downloading":
            downloaded[progress.get("filename") or ""] = progress.get("downloaded_bytes") or 0
            if (total := sum(downloaded.values())) > limit:
                raise _TooLarge(total)
    return hook


def _run_ytdlp_sync(
        url: str,
        temp_path: str,
        ydl_opts: dict[str, Any],
) -> tuple[Optional[Video], Optional[dict[str, Any]]]:
    """Synchronous yt-dlp extraction. Runs blocking I/O and must be called
    via asyncio.to_thread so it does not stall the event loop.

    A video larger than the size limit (`max_filesize`) is not downloaded at all if its
    size is known beforehand, and stopped once it outgrows the limit otherwise. If the
    retrieval asked for it (see `downscaling()`), a lower resolution that fits is
    downloaded instead. (`asyncio.to_thread` carries the retrieval's context over.) The metadata then
    says why the video is missing (`VIDEO_OMITTED`)."""
    with YoutubeDL(ydl_opts) as ydl:
        metadata = ydl.extract_info(url, download=False)  # Selects the formats, nothing more

    if metadata and metadata.get("_type") in ("playlist", "multi_video"):
        raise NotASingleVideo(f"{url} is a channel or playlist, not a single video.")

    if limit := ydl_opts.get("max_filesize"):
        size = _selection_size(metadata)
        if size and size > limit:
            selector = _fitting_selector(metadata, ydl_opts, limit) if downscaling() else None
            if selector is None:
                metadata[VIDEO_OMITTED] = (f"{_mb(size)}, more than the size limit of "
                                           f"{_mb(limit)}")
                return None, metadata
            ydl_opts = dict(ydl_opts, format=selector)
        ydl_opts = dict(ydl_opts, progress_hooks=[_size_guard(limit)])

    try:
        with YoutubeDL(ydl_opts) as ydl:
            metadata = ydl.process_ie_result(metadata, download=True)
    except _TooLarge as e:
        for leftover in glob.glob(glob.escape(temp_path) + ".*"):
            try:
                os.remove(leftover)
            except OSError:
                pass
        metadata[VIDEO_OMITTED] = (f"more than the size limit of {_mb(limit)} (stopped at "
                                   f"{_mb(e.args[0])})")
        return None, metadata

    video = None
    if ext := metadata.get("ext"):
        try:
            video = Video(file_path=temp_path + f".{ext}", source_url=url)
            video.relocate(move_not_copy=True)
            if is_too_small_video(video):
                logger.info(f"Left out the video of {url[:120]}: smaller than the minimum size.")
                video = None
        except FileNotFoundError:
            logger.debug(f"yt-dlp reported ext={ext} but file was not found at {temp_path}.{ext}")
        except Exception as e:
            logger.warning(f"Could not load downloaded video: {e}")

    return video, metadata


async def download_video_with_ytdlp(
        url: str,
        session: aiohttp.ClientSession,
        max_video_size: int | None = None,
        divert_when_paused: bool = True,
        **kwargs
) -> tuple[Optional[Video], Optional[Image], Optional[dict[str, Any]]]:
    """Downloads a video and (if not available or exceeds max. duration) its thumbnail, and the metadata using yt-dlp.
    @param max_video_size: Maximum video size in bytes. If the video is larger, the download will be aborted.
    @param divert_when_paused: Whether a YouTube video goes through the proxy while YouTube
        is paused for this server (see `_run_youtube()`); if not, it fails at once."""
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
            video, metadata = await _run_youtube(url, temp_path, ydl_opts, divert_when_paused)
        else:
            if route := proxy.route():  # The attempt goes through the proxy
                ydl_opts["proxy"] = route.with_auth
            # Run blocking yt-dlp work in a thread pool to avoid stalling the event loop.
            video, metadata = await asyncio.to_thread(_run_ytdlp_sync, url, temp_path, ydl_opts)

        if video and metadata.get("acodec") in (None, "none"):
            if ffmpeg:
                # FFmpeg was there to merge an audio stream: the source had none to offer
                # (e.g. a silent clip)
                logger.debug(f"Downloaded {video.reference}; its source has no audio stream.")
            else:
                logger.info(f"⚠️ Downloaded {video.reference} without audio. Install FFmpeg "
                            f"to enable merging the separate video and audio streams.")

        if video and max_video_size and video.size > max_video_size:
            logger.info(f"Removing video {video.reference} because it exceeds the maximum size "
                        f"of {max_video_size / 1024 / 1024:.2f} MB.")
            metadata[VIDEO_OMITTED] = (f"{_mb(video.size)}, more than the size limit of "
                                       f"{_mb(max_video_size)}")
            video = None  # Discard video

        thumbnail = None
        if not video:
            thumbnail_url = metadata.get('thumbnail')
            if thumbnail_url:
                thumbnail = await download_image(thumbnail_url, session)

        return video, thumbnail, metadata

    except (NotASingleVideo, RateLimitError, AccessBlockedError, RetrievalFailed,
            TargetUnavailableError, YtDlpFailed):
        raise  # Already classified (the YouTube path classifies its own errors)
    except YoutubeDLError as e:
        raise _classify(e) from e
    except Exception as e:
        raise RuntimeError(f"Could not download video with yt-dlp: {e}") from e


async def _run_youtube(url: str, temp_path: str, ydl_opts: dict[str, Any],
                       divert_when_paused: bool = True
                       ) -> tuple[Optional[Video], Optional[dict[str, Any]]]:
    """Downloads from YouTube. A direct format refused with HTTP 403 is retried once as
    HLS. When YouTube refuses this server (bot check, refused stream), all of YouTube is
    paused and the refusal raised; while paused, the call fails at once. Through the proxy
    if the attempt goes through it, or if YouTube is paused and a proxy is in use (unless
    `divert_when_paused` is off); through the proxy, a refused client group hands over to
    the next one, and nothing is paused.

    In a "youtube" slot, which paces the starts; the YouTube integration's retrieval
    holds it already."""
    async with slots.hold(YOUTUBE_SLOT):
        return await _download_youtube(url, temp_path, ydl_opts, divert_when_paused)


async def _download_youtube(url: str, temp_path: str, ydl_opts: dict[str, Any],
                            divert_when_paused: bool
                            ) -> tuple[Optional[Video], Optional[dict[str, Any]]]:
    ydl_opts = dict(ydl_opts, format=_youtube_format_selector(),
                    # IPv4 only: YouTube judges an IPv6 address together with its whole
                    # /64, and more harshly
                    source_address="0.0.0.0",
                    # A refused stream answers every fragment 404: give up on the first
                    # one instead of retrying (10 times by default) and skipping each of
                    # hundreds, which took 40 s before "the downloaded file is empty"
                    skip_unavailable_fragments=False, fragment_retries=2,
                    retries=1, extractor_retries=1, socket_timeout=15)
    route = proxy.route()
    if route is None and divert_when_paused:
        if youtube_gate.paused() and (route := proxy.divert(YOUTUBE)):
            logger.info(f"🔀 YouTube is paused for this server's address; retrieving {url} "
                        f"through the proxy {route.url}.")
        elif slots.started_unpaced(YOUTUBE_SLOT) and (route := proxy.divert(YOUTUBE)):
            # Started beside this server's paced lane: in parallel, through a proxy
            logger.debug(f"🔀 Retrieving {url} through the proxy {route.url}, in parallel "
                         f"to this server's own YouTube pace.")
    if route is not None:
        # The proxy's address is not the one YouTube flagged: every client group, no
        # pacing, and a refusal pauses nothing
        ydl_opts["proxy"] = route.with_auth
        groups = list(YOUTUBE_CLIENT_GROUPS)
    else:
        youtube_gate.check()  # Paused: fails at once
        groups = list(YOUTUBE_CLIENT_GROUPS[:1])
    last_error: Optional[Exception] = None
    for group in groups:
        extractor_args: dict[str, Any] = dict(youtube=dict(player_client=list(group)))
        if os.path.isdir(BGUTIL_SERVER_HOME):
            extractor_args["youtubepot-bgutilscript"] = dict(server_home=[BGUTIL_SERVER_HOME])
        opts = dict(ydl_opts, extractor_args=extractor_args)
        for hls_only in (False, True):
            if hls_only:
                opts = dict(opts, format=_YOUTUBE_HLS_ONLY)
            try:
                return await asyncio.to_thread(_run_ytdlp_sync, url, temp_path, opts)
            except YoutubeDLError as e:
                classified = _classify(e)
                if hls_only and "requested format is not available" in str(classified).lower():
                    # This client has no HLS formats: its 403 on the direct ones stands
                    classified = last_error
                last_error = classified
                bot_wall = _is_bot_wall(classified)
                if not bot_wall and not _stream_refused(classified):
                    raise classified from e
                if not bot_wall and not hls_only and "http error 403" in str(classified).lower():
                    logger.info(f"YouTube refused a direct format of {url} (HTTP 403); "
                                f"retrying with HLS only.")
                    continue
                logger.info(f"YouTube refused {url} to the {'/'.join(group)} client"
                            f"{' through the proxy' if route else ''}: {classified}")
                if route is None:
                    # This server's address is flagged: all of YouTube pauses
                    youtube_gate.trip(str(classified).split(":")[0] if bot_wall
                                      else "it refused the video's stream")
                    raise classified from e
                break  # Through the proxy: the next client group may get through
    # Through a proxy, every client group was refused too: the engine has that proxy
    # rest from YouTube and tries the next (see `proxy.learn()`)
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

    omitted = f"\nVideo omitted: {reason}" if (reason := metadata.get(VIDEO_OMITTED)) else ""

    text = f"""**{platform} Video**
Author: @{uploader}
Posted: {formatted_date}
Duration: {duration}s
Views: {fmt_count(view_count)} - Likes: {fmt_count(like_count)} - Comments: {fmt_count(comment_count)}{omitted}

{description}"""

    items: list = [text]
    if video:
        items.append(video)
    elif thumbnail:  # Only add thumbnail if video could not be downloaded
        items.append(thumbnail)

    return MultimodalSequence(items)


# A channel's page, with or without a tab (/videos, /shorts, ...), or a playlist: listed,
# its videos' thumbnails instead of their streams (see `get_youtube_listing()`)
_CHANNEL_PATH = re.compile(r"^/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)"
                           r"(/(videos|shorts|streams|featured|playlists|search))?/?$")
_CHANNEL_TABS = ("videos", "shorts", "streams")
DEFAULT_CHANNEL_VIDEOS = 30


def is_youtube_listing(url: str) -> bool:
    """Whether `url` is a YouTube channel or playlist page rather than a video."""
    parts = urlsplit(url)
    if not parts.netloc.lower().removeprefix("www.").removeprefix("m.").endswith("youtube.com"):
        return False
    return bool(_CHANNEL_PATH.match(parts.path)) or (
        parts.path.rstrip("/") == "/playlist" and "list=" in parts.query)


def _listing_url(url: str) -> str:
    """A channel's videos tab for a channel's home (or another tab without videos)."""
    parts = urlsplit(url)
    m = _CHANNEL_PATH.match(parts.path)
    if m and m.group(3) not in _CHANNEL_TABS:
        return f"https://www.youtube.com/{m.group(1)}/videos"
    return url


def _thumbnail_url(entry: dict) -> Optional[str]:
    """A video's thumbnail of at most 720 px width, the largest such."""
    thumbnails = [t for t in entry.get("thumbnails") or [] if t.get("url")]
    fitting = [t for t in thumbnails if (t.get("width") or 0) <= 720]
    if fitting or thumbnails:
        return max(fitting or thumbnails, key=lambda t: t.get("width") or 0)["url"]
    return f"https://i.ytimg.com/vi/{entry['id']}/hqdefault.jpg" if entry.get("id") else None


def _clock(seconds) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return ""
    seconds = int(seconds)
    h, m, s = seconds // 3600, seconds // 60 % 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def get_youtube_listing(url: str, session: Optional[aiohttp.ClientSession] = None
                              ) -> MultimodalSequence:
    """A channel's or playlist's page: its name, subscribers and description, then its
    latest videos (`youtube_channel_videos`), each with its title, link, duration and
    views, and its thumbnail -- not its stream. The listing is one request of YouTube's
    own web API (half a second), which YouTube's bot check does not guard."""
    limit = max(1, int(get_setting("youtube_channel_videos", DEFAULT_CHANNEL_VIDEOS)))
    opts: dict[str, Any] = dict(quiet=True, logger=logger_yt_dlp, extract_flat=True,
                                playlistend=limit, skip_download=True,
                                source_address="0.0.0.0", socket_timeout=15)
    if route := proxy.route():
        opts["proxy"] = route.with_auth

    def extract() -> dict:
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(_listing_url(url), download=False)
    try:
        info = await asyncio.to_thread(extract)
    except YoutubeDLError as e:
        raise _classify(e) from e
    if not info:
        raise RetrievalFailed(f"YouTube listed nothing for {url}.")
    entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)][:limit]

    async def thumbnail(entry: dict) -> Optional[Image]:
        if not (source := _thumbnail_url(entry)):
            return None
        try:
            return await download_image(source, session=session, ignore_small_images=False)
        except Exception:
            logger.debug(f"Could not download the thumbnail {source}.", exc_info=True)
            return None
    images = await asyncio.gather(*(thumbnail(e) for e in entries))

    is_playlist = "list=" in urlsplit(url).query
    name = info.get("channel") or info.get("uploader") or info.get("title") or ""
    lines = [f"**YouTube {'Playlist' if is_playlist else 'Channel'}**"]
    if is_playlist and info.get("title"):
        lines.append(f"Title: {info['title']}")
    lines.append(f"Channel: {name}" + (f" ({info['uploader_id']})" if info.get("uploader_id") else ""))
    if isinstance(info.get("channel_follower_count"), int):
        lines.append(f"Subscribers: {fmt_count(info['channel_follower_count'])}")
    if info.get("description"):
        lines += ["", info["description"]]
    lines += ["", f"Latest {len(entries)} videos:" if not is_playlist else f"{len(entries)} videos:"]
    items: list = ["\n".join(lines)]
    for i, (entry, image) in enumerate(zip(entries, images), 1):
        details = [d for d in (_clock(entry.get("duration")),
                               f"{fmt_count(entry['view_count'])} views"
                               if isinstance(entry.get("view_count"), int) else "") if d]
        link = entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id')}"
        items.append(f"\n{i}. {entry.get('title') or 'Untitled'} -- {link}"
                     + (f" ({', '.join(details)})" if details else ""))
        if image is not None:
            items.append(image)
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
