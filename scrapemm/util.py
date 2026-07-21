import asyncio
import base64
import binascii
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Awaitable, Iterable, Union
from urllib.parse import unquote

import aiohttp
import tqdm
from PIL import UnidentifiedImageError
from bs4 import BeautifulSoup, Tag
from ezmm import MultimodalSequence, Item, Image, Video
from markdownify import markdownify as md
from playwright.async_api import APIRequestContext, Page, Frame

from scrapemm.download import download_video, download_image
from scrapemm.download.images import image_from_binary
from scrapemm.download.util import (
    looks_like_image_file_url,
    looks_like_vector_file_url,
)
from scrapemm.download.videos import video_from_binary, download_hls_video, is_hls

logger = logging.getLogger("scrapeMM")

DOMAIN_REGEX = r"(?:https?:\/\/)?(?:www\.)?([-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6})/?"


def preprocess_url(url: str) -> str:
    """Decodes a URL and removes unwanted symbols from it such
    as surrounding whitespace, non-breaking spaces, etc."""
    return unquote(str(url)).strip()


def get_domain(url: str, keep_subdomain: bool = False) -> Optional[str]:
    """Uses regex to get out the domain from the given URL. The output will be
    of the form 'example.com'. No 'www', no 'http'."""
    url = str(url)
    match = re.search(DOMAIN_REGEX, url)
    if match:
        domain = match.group(1)
        if not keep_subdomain:
            # Keep only second-level and top-level domain
            domain = '.'.join(domain.split('.')[-2:])
        return domain


async def run_with_semaphore(tasks: Iterable[Awaitable],
                             limit: int,
                             show_progress: bool = True,
                             progress_description: str | None = None) -> tuple:
    """
    Runs asynchronous tasks with a concurrency limit.

    Args:
        tasks: The tasks to execute concurrently.
        limit: The maximum number of coroutines to run concurrently.
        show_progress: Whether to show a progress bar while executing tasks.
        progress_description: The message to display in the progress bar.

    Returns:
        list: A list of results returned by the tasks, order-preserved.
    """
    semaphore = asyncio.Semaphore(limit)  # Limit concurrent executions

    async def limited_coroutine(t: Awaitable):
        try:
            async with semaphore:
                return await t
        except asyncio.CancelledError:
            if hasattr(t, "close"):
                t.close()
            raise

    tasks: list = [asyncio.create_task(limited_coroutine(task)) for task in tasks]

    # Report completion status of tasks (if more than one task)
    if show_progress:
        progress = tqdm.tqdm(total=len(tasks), desc=progress_description, file=sys.stdout)
        while progress.n < len(tasks):
            progress.n = sum(task.done() for task in tasks)
            progress.refresh()
            await asyncio.sleep(0.1)
        progress.close()

    return await asyncio.gather(*tasks)


def read_urls_from_file(file_path):
    with open(file_path, 'r') as f:
        return f.read().splitlines()


def get_user_input(prompt: str, multiline: bool = False) -> str:
    """Prompts the user for input.
    - Single-line: uses standard input(); Enter submits, empty input skips.
    - Multiline: uses prompt_toolkit; Enter adds newline, Alt+Enter submits.
    """
    if multiline:
        from prompt_toolkit import prompt as pt_prompt
        from prompt_toolkit.key_binding import KeyBindings

        print(prompt)

        bindings = KeyBindings()

        @bindings.add('escape', 'enter')
        def _submit(event):
            event.current_buffer.validate_and_handle()

        result = pt_prompt(
            ">>> ",
            multiline=True,
            key_bindings=bindings,
            prompt_continuation="... ",
        )
        return result
    else:
        return input(f"{prompt} ")


MAX_MEDIA_PER_PAGE = 32
URL_REGEX = r"https?:\/\/(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9@:%_\+.~#?&//=]*)"
DATA_URI_REGEX = r"data:([\w/+.-]+/[\w.+-]+);base64,([A-Za-z0-9+/=]+)"
MD_HYPERLINK_REGEX = rf'(!?\[([^]^[]*)\]\((.*?)(?: "[^"]*")?\))'


def preprocess_html(html: str) -> str:
    # Resolve base64-encoded text sequences
    data_uris = re.findall(DATA_URI_REGEX, html)
    for mime_type, base64_encoding in data_uris:
        if mime_type.startswith("text/"):
            try:
                decoded_text = base64.b64decode(base64_encoding).decode('utf-8')
                html = html.replace(f"data:{mime_type};base64,{base64_encoding}", decoded_text)
            except (binascii.Error, UnicodeDecodeError):
                continue
    return html


def postprocess_markdown(text: str) -> str:
    # Remove any excess whitespaces
    text = re.sub(r' {2,}', ' ', text)

    # Remove any excess newlines
    text = re.sub(r'(\n *){3,}', '\n\n', text)

    return sanitize(text.strip())


_BG_IMAGE_URL_RE = re.compile(
    r"background-image\s*:\s*url\(\s*['\"]?([^'\")\s]+)['\"]?\s*\)",
    re.IGNORECASE,
)


def _normalize_media_url(url: str) -> str:
    """Normalize protocol-relative URLs to https for downstream fetchers."""
    if url.startswith("//"):
        return "https:" + url
    return url


def _is_eligible_background_image_url(url: str) -> bool:
    """True if a CSS background-image URL is likely real page media (not emoji/icon)."""
    if not url or url.startswith("data:"):
        return False
    lowered = url.lower()
    if "/emoji/" in lowered or "/emojis/" in lowered:
        return False
    if looks_like_vector_file_url(url):
        return False
    if looks_like_image_file_url(url):
        return True
    # Telegram CDN and similar often serve images under /file/ without a clean extension
    # in every rewrite; accept common CDN path patterns.
    if "telegram-cdn.org/file/" in lowered or "/file/" in lowered and "cdn" in lowered:
        return True
    return False


def _background_image_url(element: Tag) -> Optional[str]:
    """Extract an eligible background-image URL from an element's inline style."""
    style = element.get("style")
    if not style:
        return None
    match = _BG_IMAGE_URL_RE.search(str(style))
    if not match:
        return None
    url = _normalize_media_url(match.group(1).strip())
    if not _is_eligible_background_image_url(url):
        return None
    return url


def _strip_background_image_style(element: Tag) -> None:
    """Remove background-image from inline style without destroying child content."""
    style = element.get("style")
    if not style:
        return
    new_style = _BG_IMAGE_URL_RE.sub("", str(style))
    new_style = re.sub(r";\s*;", ";", new_style).strip(" ;")
    if new_style:
        element["style"] = new_style
    elif element.has_attr("style"):
        del element["style"]
    if element.has_attr("src") and not element.name in ("img", "video", "source"):
        del element["src"]


def _extract_media_elements(soup: BeautifulSoup) -> list[Tag]:
    """Identifies all potential media elements and their URIs in the soup."""
    media_elements = []
    seen_ids: set[int] = set()

    def _add(element: Tag) -> None:
        eid = id(element)
        if eid not in seen_ids:
            seen_ids.add(eid)
            media_elements.append(element)

    for element in soup.find_all("img"):
        src = str(element.get("src"))
        # Skip vector graphics
        if src and looks_like_vector_file_url(src):
            continue
        _add(element)

    # CSS background images used as primary media (e.g. Telegram photo wraps).
    # Only leaf hosts — never page wrappers that contain the rest of the document.
    for element in soup.find_all(style=True):
        if element.name in ("img", "video", "source"):
            continue
        bg_url = _background_image_url(element)
        if not bg_url:
            continue
        # Wire URI through existing src-based resolve_media path
        if not element.get("src"):
            element["src"] = bg_url
        _add(element)

    # For videos, include either the src attribute (higher precedence) or the first source element
    for video in soup.find_all("video"):
        if video.has_attr("src"):
            _add(video)
        elif source := video.find("source"):
            _add(source)
            # In the HTML DOM, replace the video node with the source node to ensure a clean output
            video.replace_with(source)

    return media_elements


def _resolve_base64_media(
        media_elements_uris: list[tuple[Tag, Optional[str]]],
        source_url: str | None = None
) -> list[Optional[Item]]:
    """Resolves all base64-encoded media elements.
    Returns a list of (element, Item) for the resolved media and removes them from media_elements."""
    resolved = []
    # Using a while loop or iterating over a copy to safely remove from the original list
    for element, uri in media_elements_uris:
        if uri and is_data_uri(uri):
            data_uri_info = decompose_data_uri(uri)
            if data_uri_info:
                mime_type, base64_encoding = data_uri_info
                medium = from_base64(base64_encoding, mime_type=mime_type, url=source_url)
                if medium:
                    resolved.append(medium)
                    continue
        resolved.append(None)
    return resolved


async def resolve_media(
        html: str,
        session: Union[aiohttp.ClientSession, "APIRequestContext"],
        url: str | None = None,
        source_element: Union[Frame, Page, None] = None,
        **kwargs
) -> Optional[MultimodalSequence]:
    """Downloads all media that are contained in the provided HTML.
    Removes images that are smaller than 256 x 256. Replaces the
    respective HTML elements with their proper item reference."""

    if html is None:
        return None

    soup = BeautifulSoup(html, "html.parser")
    domain_root = get_domain_root(url) if url else None

    # 1. Identify all potential media elements and their URLs
    media_elements: list[Tag] = _extract_media_elements(soup)
    if not media_elements:
        return MultimodalSequence(html)

    media_uris: list[Optional[str]] = [str(element.get("src")) if element.get("src") else None
                                       for element in media_elements]

    # 2. Resolve base64 media
    resolved_media: list[Optional[Item]] = _resolve_base64_media(list(zip(media_elements, media_uris)), source_url=url)

    # 3. Normalize URLs and prepare tasks for remaining elements
    tasks = []
    unique_urls = []  # We use a list to map normalized URLs to their download result to avoid duplicate downloads

    # Normalize URLs in URI list
    for i, uri in enumerate(media_uris):
        if uri and domain_root and is_root_relative_url(uri):
            media_uris[i] = f"{domain_root}{uri}"

    # Create retrieval tasks for URL elements
    for element, uri in zip(media_elements, media_uris):
        if uri and is_url(uri) and uri not in unique_urls:
            if element.name in ["video", "source"]:
                if source_element:
                    tasks.append(fetch_video_via_page(source_element, uri))
                else:
                    tasks.append(
                        download_video(uri, session=session, headers={"Referer": url} if url else {}, **kwargs))
            else:  # It's an image
                if source_element:
                    tasks.append(fetch_image_via_page(source_element, uri, **kwargs))
                else:
                    tasks.append(
                        download_image(uri, session=session, headers={"Referer": url} if url else {}, **kwargs))
            unique_urls.append(uri)

    # 4. Download media
    media_results = await run_with_semaphore(tasks, limit=20, show_progress=False)
    url_to_medium = dict(zip(unique_urls, media_results))

    # 5. Add downloaded media to resolved_media
    for i, uri in enumerate(media_uris):
        if medium := url_to_medium.get(uri):
            resolved_media[i] = medium

    # 6. Replace or remove elements in the SOUP
    for element, medium in zip(media_elements, resolved_media):
        # Check if element is still in the tree
        if element.parent is None:
            continue

        has_child_tags = any(getattr(child, "name", None) for child in element.children)

        if medium:
            too_small = isinstance(medium, Image) and (medium.width < 256 or medium.height < 256)

            if not too_small:
                if has_child_tags:
                    # Keep wrapper markup; insert the resolved medium before it.
                    _strip_background_image_style(element)
                    element.insert_before(medium.reference)
                else:
                    element.replace_with(medium.reference)
                continue

        # No media retrieved. Remove element if not a container
        if has_child_tags:
            _strip_background_image_style(element)
        else:
            element.decompose()

    return MultimodalSequence(str(soup))


# Fetches a resource from within the frame so replay/archive service workers
# intercept it (the shared request context and top document escape that scope).
# The Blob is encoded via the browser-native FileReader; Blobs are disk-backed in
# Chromium, so this stays memory-friendly for large video. Base64 is required
# because Playwright's evaluate can only return JSON-serializable values, so
# binary cannot cross the CDP boundary directly.
_IN_FRAME_FETCH_JS = """
async (url) => {
    const resp = await fetch(url, { credentials: 'include' });
    if (!resp.ok) return { ok: false, status: resp.status };
    const blob = await resp.blob();
    const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(blob);
    });
    return { ok: true, dataUrl, contentType: resp.headers.get('content-type') };
}
"""


async def _retrieve_media_bytes(
        source_element: Union[Frame, Page],
        url: str,
        timeout: float = 180.0,
) -> tuple[Optional[bytes], Optional[str]]:
    """Retrieves the raw bytes of a media URL using the browser's authenticated
    session, without navigating a tab (which hangs forever on streamed video).

    Two strategies are tried, in order of archive/anti-bot fidelity:
      1. In-frame ``fetch()`` executed inside ``source_element`` (the frame that
         renders the media). Running the request in that exact frame is essential
         for replay-based archives (e.g. Ghostarchive / ReplayWeb.page), where
         archived media is served by a service worker whose scope only covers the
         replay ``iframe``; a request from the top document or from the shared
         request context escapes that scope and 404s against the live web.
      2. Playwright's shared ``APIRequestContext`` (``context.request``), which
         reuses the browser context's cookies. Used as a fallback for cross-origin
         media the in-frame fetch cannot read (e.g. blocked by CORS).

    Returns a ``(content, content_type)`` tuple; ``content`` is ``None`` on failure.
    """
    # Strategy 1: in-frame fetch inside the frame that renders the media
    try:
        result = await asyncio.wait_for(source_element.evaluate(_IN_FRAME_FETCH_JS, url), timeout=timeout)
        if result and result.get("ok"):
            content = base64.b64decode(result["dataUrl"].partition(",")[2])
            if content:
                logger.debug(f"Retrieved {url} via in-frame fetch ({len(content)} bytes)")
                return content, result.get("contentType")
        else:
            logger.debug(f"In-frame fetch failed for {url}: {result}")
    except asyncio.TimeoutError:
        logger.debug(f"In-frame fetch timed out for {url}")
    except Exception:
        logger.debug(f"In-frame fetch error for {url}", exc_info=True)

    # Strategy 2: shared request context (for cross-origin media not reachable in-frame)
    page = source_element if isinstance(source_element, Page) else source_element.page
    try:
        response = await page.context.request.get(url, timeout=timeout * 1000)
        if response.ok:
            content = await response.body()
            logger.debug(f"Retrieved {url} via request context ({len(content)} bytes)")
            return content, response.headers.get("content-type")
        logger.debug(f"Request-context fetch failed for {url}: HTTP {response.status}")
    except Exception:
        logger.debug(f"Request-context fetch error for {url}", exc_info=True)

    return None, None


async def get_url_content_via_page(source_element: Union[Frame, Page], url: str) -> Optional[bytes]:
    content, _ = await _retrieve_media_bytes(source_element, url)
    return content


async def fetch_image_via_page(source_element: Union[Frame, Page], url: str, **kwargs) -> Optional[Image]:
    content = await get_url_content_via_page(source_element, url)
    return image_from_binary(content, source_url=url, **kwargs) if content else None


async def fetch_video_via_page(
        source_element: Union[Frame, Page],
        url: str,
        timeout: float = 180.0,
) -> Optional[Video]:
    content, content_type = await _retrieve_media_bytes(source_element, url, timeout=timeout)

    # HLS playlists are plain text manifests, not raw video: remux via ffmpeg,
    # reusing the shared request context so segment downloads stay authenticated.
    if content_type and is_hls(content_type):
        page = source_element if isinstance(source_element, Page) else source_element.page
        return await download_hls_video(url, session=page.context.request)

    return video_from_binary(content, source_url=url) if content else None


def is_url(href: str) -> bool:
    """Returns True iff the given string is an absolute HTTP URL."""
    return re.match(URL_REGEX, href) is not None


def is_root_relative_url(href: str) -> bool:
    """Returns True iff the given string is a root-relative URL."""
    return href.startswith("/")


def is_data_uri(href: str) -> bool:
    """Returns True iff the given string is a valid data URI."""
    return re.match(DATA_URI_REGEX, href) is not None


def get_domain_root(url: str) -> Optional[str]:
    """Extracts the domain root from the given URL. Allows for missing http(s) prefix."""
    match = re.match(r"(:?https?://)?([^/]+)", url)
    if match:
        return match.group(0)
    else:
        return None


def get_markdown_hyperlinks(text: str) -> list[tuple[str, str, str]]:
    """Extracts all web hyperlinks from the given markdown-formatted string. Returns
    a list of fullmatch-hypertext-URL-triples."""
    pattern = re.compile(MD_HYPERLINK_REGEX, re.DOTALL)
    hyperlinks = re.findall(pattern, text)
    return hyperlinks


def decompose_data_uri(href: str) -> Optional[tuple[str, str]]:
    """Extracts the mime type and base64-encoded data from a data URI."""
    match = re.match(DATA_URI_REGEX, href)
    if match:
        return match.group(1), match.group(2)
    else:
        return None


async def to_multimodal_sequence(
        html: str | None,
        session: Union[aiohttp.ClientSession, "APIRequestContext"],
        **kwargs
) -> Optional[MultimodalSequence]:
    """Turns scraped HTML content into the corresponding MultimodalSequences
    by resolving media hyperlinks and Base64 encodings and converting to Markdown."""
    if html is None:
        return None

    # 0. Preprocess HTML
    html = preprocess_html(html)
    assert html is not None

    # 1. Resolve media in HTML
    mms = await resolve_media(html, session=session, **kwargs)
    if not mms:
        return None

    # 2. Convert resulting (partially replaced) HTML to Markdown
    text = html2md(mms)

    return MultimodalSequence(text)


def html2md(html: str | MultimodalSequence) -> str:
    """Converts HTML to Markdown."""
    try:
        markdown = md(str(html), heading_style="ATX")
        return postprocess_markdown(markdown)
    except RecursionError:
        logger.debug("RecursionError while converting HTML to Markdown.")
        raise


def sanitize(text: str) -> str:
    """Post-processes scraped text, removing invalid characters."""
    return text.replace("\u0000", "")


def from_base64(b64_data: str, mime_type: str = "image/jpeg", url: str | None = None) -> Optional[Item]:
    """Converts a base64-encoded image or video to an Item object."""
    try:
        binary_data = base64.b64decode(b64_data, validate=True)
        if binary_data:
            if mime_type == "image/svg+xml":
                return None  # We do not care about SVGs
            elif mime_type.startswith("image/"):
                return Image(binary_data=binary_data, source_url=url)
            elif mime_type.startswith("video/"):
                return Video(binary_data=binary_data, source_url=url)
            else:
                raise ValueError(f"Unsupported base64 mime type: {mime_type}")
    except binascii.Error:  # base64 validation failed
        return None
    except UnidentifiedImageError:  # Pillow could not identify image format
        return None
    except Exception as e:
        logger.debug(f"Error decoding {mime_type} base64 data. \n {type(e).__name__}: {e}")


def normalize_video(video: Video):
    """Transcodes the video for browser playback."""
    input_path = video.file_path
    output_path = input_path.with_suffix(".normalized.mp4")

    try:
        meta = probe_video(input_path)
        if not meta:
            return

        # Case 1: fully browser-safe → only ensure faststart
        if is_browser_safe(meta):
            run_command([
                "ffmpeg",
                "-y",
                "-i", str(input_path),
                "-c", "copy",
                "-movflags", "+faststart",
                str(output_path),
            ])
            return output_path

        # Case 2: re-encode to canonical browser format
        run_command([
            "ffmpeg",
            "-y",
            "-i", str(input_path),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-profile:v", "main",
            "-level", "4.1",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_path),
        ])
    except subprocess.CalledProcessError as e:
        logger.warning(f"Error normalizing video {input_path}: {e}")


def probe_video(path: Path) -> dict | None:
    """Return ffprobe JSON metadata."""
    result = run_command([
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(path),
    ])
    if result:
        return json.loads(result.stdout)


def is_browser_safe(meta: dict) -> bool:
    """Check whether the video is safely playable in browsers."""
    video_ok = False
    audio_ok = False

    for stream in meta.get("streams", []):
        if stream["codec_type"] == "video":
            codec = stream.get("codec_name")
            pix_fmt = stream.get("pix_fmt", "")
            profile = stream.get("profile", "")

            video_ok = (
                    codec == "h264"
                    and pix_fmt == "yuv420p"
                    and "High 10" not in profile
            )

        if stream["codec_type"] == "audio":
            audio_ok = stream.get("codec_name") in {"aac", "mp3"}

    return video_ok and audio_ok


def run_command(cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
    except UnicodeDecodeError:
        logger.debug(f"Error running command {cmd}: Unicode decoding failed")
        return None


def parse_netscape_cookies(cookie_file: Path) -> list[dict]:
    """Parse the Netscape-format cookie file and return cookie dicts.

    Netscape format fields (tab-separated):
        domain  include_subdomains  path  is_secure  expiry  name  value
    """
    if not cookie_file.exists():
        return []

    cookies = []
    with open(cookie_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _include_subdomains, path, is_secure, expiry, name, value = parts[:7]
            try:
                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path,
                    "expires": int(expiry),
                    "httpOnly": False,
                    "secure": is_secure.upper() == "TRUE",
                    "sameSite": "None",
                })
            except ValueError:
                continue
    return cookies


async def unshorten(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    """Expands short URLs to their full form, e.g., URLs from tinyurl.com, bit.ly,
    goo.gl, youtu.be, t.ly, t.co, etc."""
    try:
        async with session.get(url, allow_redirects=True) as resp:
            expanded = str(resp.url)
            if expanded.rstrip("/") != str(url).rstrip("/"):
                return expanded
            # t.co (and similar) return 200 with a meta-refresh instead of a 3xx redirect.
            match = re.search(r"URL=(https?://[^\"'>\s]+)", await resp.text(), re.I)
            return match.group(1) if match else None
    except Exception:
        return None
