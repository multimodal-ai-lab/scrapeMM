import asyncio
import base64
import binascii
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Awaitable, Iterable, Union, TYPE_CHECKING
from urllib.parse import unquote, urljoin

import aiohttp
import tqdm
from PIL import UnidentifiedImageError
from bs4 import BeautifulSoup, Tag
from ezmm import MultimodalSequence, Item, Image, Video
from markdownify import markdownify as md
from playwright.async_api import APIRequestContext, Page, Frame

from scrapemm.server.download import download_video, download_image
from scrapemm.server.download.images import image_from_binary
from scrapemm.server.download.util import (
    looks_like_image_file_url,
    looks_like_vector_file_url,
    looks_like_video_embed_url,
)
from scrapemm.server.download.videos import (video_from_binary, download_hls_video, is_hls,
                                      _resolve_ffmpeg_path, _resolve_ffprobe_path)

if TYPE_CHECKING:
    from scrapemm.common.scraping_response import ScrapedContent, OutputFormat

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


MAX_MEDIA_PER_PAGE = 32
URL_REGEX = r"https?:\/\/(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9@:%_\+.~#?&//=]*)"
DATA_URI_REGEX = r"data:([\w/+.-]+/[\w.+-]+);base64,([A-Za-z0-9+/=]+)"
MD_HYPERLINK_REGEX = rf'(!?\[([^]^[]*)\]\((.*?)(?: "[^"]*")?\))'
MD_DATA_URI_LINK_REGEX = r'!?\[[^]^[]*\]\(\s*data:[^)]*\)'

# Marks HttpOnly cookies in the Netscape cookies.txt format
HTTP_ONLY_PREFIX = "#HttpOnly_"

# Maps the SameSite spellings found in cookie exports to Playwright's values
SAME_SITE_VALUES = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",  # Cookie-Editor's spelling
}


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
    # Media worth keeping was already turned into items; any base64 left over is
    # unresolvable or too small, and only bloats the text
    text = re.sub(MD_DATA_URI_LINK_REGEX, "", text)
    text = re.sub(DATA_URI_REGEX, "", text)

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


# Lazy-loading plugins park the real image URL in a data attribute and leave `src`
# pointing at a placeholder. Ordered by how specific the attribute is.
LAZY_SRC_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-original-src",
                  "data-image-src", "data-hi-res-src", "data-full-src", "data-echo",
                  "data-litespeed-src")
LAZY_SRCSET_ATTRS = ("srcset", "data-srcset", "data-lazy-srcset")

# Placeholder `src` values that lazy loaders use until the real image is swapped in
_PLACEHOLDER_HINTS = ("placeholder", "blank.gif", "blank.png", "spacer.gif",
                      "lazy.gif", "loader.gif", "transparent.png", "grey.gif")


def _largest_srcset_candidate(srcset: str) -> Optional[str]:
    """Picks the highest-resolution URL out of a srcset attribute. Candidates are
    'url [<n>w|<n>x]' pairs; the descriptor is optional."""
    best_url, best_weight = None, -1.0
    for candidate in srcset.split(","):
        parts = candidate.strip().split()
        if not parts:
            continue
        url, descriptor = parts[0], parts[1] if len(parts) > 1 else ""
        if _is_placeholder_src(url):
            continue  # Some lazy loaders fill srcset with placeholders too
        try:
            # 'w' describes pixel width, 'x' pixel density; both are "bigger is better"
            weight = float(descriptor[:-1]) if descriptor[-1:] in ("w", "x") else 1.0
        except ValueError:
            weight = 1.0
        if weight > best_weight:
            best_url, best_weight = url, weight
    return best_url


def _is_placeholder_src(src: str) -> bool:
    """True iff `src` looks like a stand-in that a lazy loader replaces at runtime."""
    if not src:
        return True
    lowered = src.lower()
    # Inline data URIs are the classic placeholder, but can also be the real image.
    # Callers only prefer an alternative when one actually exists, so this is safe.
    if lowered.startswith("data:"):
        return True
    return any(hint in lowered for hint in _PLACEHOLDER_HINTS)


def _best_image_src(element: Tag) -> Optional[str]:
    """Returns the best available source URL of an <img>.

    Server-rendered HTML rarely carries the final image in `src`: lazy loaders keep it
    in a data attribute until the element scrolls into view, and responsive images
    offer several resolutions via srcset. Taking `src` alone therefore yields
    placeholders or needlessly small variants.
    """
    src = element.get("src")
    src = str(src).strip() if src else ""

    # Highest-resolution responsive variant, if the element offers one
    for attr in LAZY_SRCSET_ATTRS:
        if srcset := element.get(attr):
            if candidate := _largest_srcset_candidate(str(srcset)):
                return candidate

    # A real src beats any data attribute
    if src and not _is_placeholder_src(src):
        return src

    # Otherwise fall back to whatever the lazy loader stashed away
    for attr in LAZY_SRC_ATTRS:
        if value := element.get(attr):
            value = str(value).strip()
            if value and not _is_placeholder_src(value):
                return value

    return src or None


def _resolve_media_url(uri: str, page_url: Optional[str], domain_root: Optional[str]) -> str:
    """Turns a media reference found in the page into an absolute URL.

    Handles protocol-relative (`//cdn/x.jpg`), root-relative (`/x.jpg`) and
    document-relative (`img/x.jpg`) references. Data URIs and already-absolute URLs
    are returned unchanged.
    """
    uri = uri.strip()
    if not uri or uri.startswith("data:"):
        return uri
    if uri.startswith("//"):
        return _normalize_media_url(uri)
    if is_url(uri):
        return uri
    if page_url:
        # urljoin resolves every relative form against the page it was found on
        return urljoin(page_url, uri)
    if domain_root and is_root_relative_url(uri):
        return f"{domain_root}{uri}"
    return uri


def _best_video_src(element: Tag) -> Optional[str]:
    """Returns the best available source URL of a <video> or <source>. Players are
    lazy-loaded just like images are, so `src` alone is not enough."""
    src = element.get("src")
    src = str(src).strip() if src else ""
    if src and not _is_placeholder_src(src):
        return src
    for attr in LAZY_SRC_ATTRS:
        if value := element.get(attr):
            value = str(value).strip()
            if value and not _is_placeholder_src(value):
                return value
    return src or None


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
        src = _best_image_src(element)
        # Skip vector graphics
        if src and looks_like_vector_file_url(src):
            continue
        if src:
            # Wire the resolved URI through the existing src-based resolve_media path
            element["src"] = src
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

    # Embedded players (YouTube, Vimeo, ...) carry the page's video content just as much
    # as a <video> tag does, they just need yt-dlp to be downloaded.
    for element in soup.find_all("iframe"):
        # Lazy-loading plugins park the real URL in a data attribute
        src = element.get("src") or element.get("data-src") or element.get("data-litespeed-src")
        if src and looks_like_video_embed_url(str(src)):
            element["src"] = str(src)
            _add(element)

    # For videos, include either the src attribute (higher precedence) or the first source element
    for video in soup.find_all("video"):
        if src := _best_video_src(video):
            video["src"] = src
            _add(video)
        elif source := video.find("source"):
            if src := _best_video_src(source):
                source["src"] = src
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


async def download_embedded_video(
        url: str,
        session: Union[aiohttp.ClientSession, "APIRequestContext"],
        max_video_size: Optional[int] = None,
        **kwargs
) -> Optional[Video]:
    """Downloads the video behind an embedded player with yt-dlp. Returns None if that
    fails: an embedded video is a bonus, so it must never fail the whole page."""
    from scrapemm.server.integrations.ytdlp import download_video_with_ytdlp

    try:
        video, _thumbnail, _metadata = await download_video_with_ytdlp(
            url, session=session, max_video_size=max_video_size)
        return video
    except Exception as e:
        logger.info(f"Could not download the video embedded from {url}: "
                    f"{type(e).__name__}: {e}")
        return None


async def resolve_media(
        html: str,
        session: Union[aiohttp.ClientSession, "APIRequestContext"],
        url: str | None = None,
        source_element: Union[Frame, Page, None] = None,
        max_video_size: Optional[int] = None,
        **kwargs
) -> MultimodalSequence:
    """Downloads all media that are contained in the provided HTML.
    Removes images that are smaller than 256 x 256. Replaces the
    respective HTML elements with their proper item reference."""
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

    # Normalize URLs in URI list. Pages reference media protocol-relative (//cdn/x.jpg),
    # root-relative (/x.jpg) and document-relative (img/x.jpg); resolving only the
    # root-relative ones silently drops the rest.
    for i, uri in enumerate(media_uris):
        if uri:
            media_uris[i] = _resolve_media_url(uri, page_url=url, domain_root=domain_root)

    # Create retrieval tasks for URL elements
    for element, uri in zip(media_elements, media_uris):
        if uri and is_url(uri) and uri not in unique_urls:
            if element.name == "iframe":
                tasks.append(download_embedded_video(uri, session=session,
                                                     max_video_size=max_video_size))
            elif element.name in ["video", "source"]:
                if source_element:
                    tasks.append(fetch_video_via_page(source_element, uri))
                else:
                    tasks.append(
                        download_video(uri, session=session, max_video_size=max_video_size,
                                       headers={"Referer": url} if url else {}, **kwargs))
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
    inserted_url_refs: set[str] = set()
    for i, (element, medium) in enumerate(zip(media_elements, resolved_media)):
        # Check if element is still in the tree
        if element.parent is None:
            continue

        uri = media_uris[i]
        has_child_tags = any(getattr(child, "name", None) for child in element.children)

        if medium:
            too_small = isinstance(medium, Image) and (medium.width < 256 or medium.height < 256)

            if not too_small:
                if uri and uri in inserted_url_refs:
                    if has_child_tags:
                        _strip_background_image_style(element)
                    else:
                        element.decompose()
                    continue
                if uri:
                    inserted_url_refs.add(uri)

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


async def to_scraped_content(
        html: str,
        session: Union[aiohttp.ClientSession, "APIRequestContext"],
        output_format: "OutputFormat" = "multimodal",
        **kwargs
) -> "ScrapedContent":
    """Turns the scraped HTML into a ScrapedContent object, converting it format by
    format until the requested `output_format` is reached. That is, no work is done
    beyond what the caller asked for: Markdown is converted only if more than the raw
    HTML is needed and media is downloaded only for the 'multimodal' format."""
    from scrapemm.common.scraping_response import ScrapedContent

    content = ScrapedContent(html=html)
    if output_format == "html":
        return content

    content.markdown = html2md(html)
    if output_format == "markdown":
        return content

    content.multimodal = await to_multimodal_sequence(html, session=session, **kwargs)
    return content


async def to_multimodal_sequence(
        html: str,
        session: Union[aiohttp.ClientSession, "APIRequestContext"],
        **kwargs
) -> MultimodalSequence:
    """Turns scraped HTML content into the corresponding MultimodalSequences
    by resolving media hyperlinks and Base64 encodings and converting to Markdown."""
    # 0. Preprocess HTML
    html = preprocess_html(html)
    assert html is not None

    # 1. Resolve media in HTML
    mms = await resolve_media(html, session=session, **kwargs)

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


async def normalize_video(video: Video) -> bool:
    """Transcodes the video in place so that browsers can play it back. Returns
    whether the video was changed.

    The transcode replaces the item's own file: writing the result to a sibling path
    and leaving the item pointing at the original would do the work without ever
    taking effect. ffmpeg runs as an async subprocess, so a re-encode does not stall
    the retrievals running concurrently on this event loop.
    """
    input_path = video.file_path
    # Write beside the original first, then swap atomically, so that a crash or a
    # failing transcode can never leave a truncated file behind.
    temp_path = input_path.with_name(f"{input_path.stem}.normalizing.mp4")

    try:
        meta = await probe_video(input_path)
        if not meta:
            return False

        if is_browser_safe(meta):
            # Already playable; only move the moov atom to the front so that players
            # can start before the whole file has arrived. Streams are copied, not
            # re-encoded, so this is cheap.
            cmd = ["-i", str(input_path), "-c", "copy", "-movflags", "+faststart"]
        else:
            # Re-encode to the canonical browser format
            cmd = [
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
            ]

        ffmpeg = _resolve_ffmpeg_path()
        if not ffmpeg:
            logger.debug("FFmpeg not found; skipping video normalization.")
            return False

        result = await run_command_async([ffmpeg, "-y", "-loglevel", "error", "-hide_banner",
                                          *cmd, str(temp_path)])
        if result is None or not temp_path.exists() or temp_path.stat().st_size == 0:
            logger.warning(f"Could not normalize video {input_path}.")
            temp_path.unlink(missing_ok=True)
            return False

        _replace_item_file(video, temp_path)
        return True

    except Exception as e:
        logger.warning(f"Error normalizing video {input_path}: {type(e).__name__}: {e}")
        temp_path.unlink(missing_ok=True)
        return False


def _replace_item_file(item: Item, new_file: Path) -> None:
    """Swaps an item's file for `new_file`, keeping the item (and the ezmm registry)
    pointing at the result. The item keeps its id and reference."""
    from ezmm.common.registry import item_registry

    target = item.file_path.with_suffix(".mp4")
    os.replace(new_file, target)
    if target != item.file_path:
        # The container changed (e.g. .webm -> .mp4), so the old file is now stale
        item.file_path.unlink(missing_ok=True)
        item.file_path = target
        item_registry.update_file_path(item)


async def run_command_async(cmd: list[str]) -> Optional[bytes]:
    """Runs a command without blocking the event loop. Returns its stdout,
    or None if the command failed."""
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
    except (FileNotFoundError, OSError) as e:
        logger.debug(f"Could not run {cmd[0]}: {e}")
        return None

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
        logger.debug(f"Command {cmd[0]} failed with code {process.returncode}: {detail}")
        return None
    return stdout


_warned_about_ffprobe = False


async def probe_video(path: Path) -> dict | None:
    """Return ffprobe JSON metadata."""
    global _warned_about_ffprobe
    ffprobe = _resolve_ffprobe_path()
    if not ffprobe:
        if not _warned_about_ffprobe:
            _warned_about_ffprobe = True
            logger.warning("⚠️ ffprobe not found, so videos cannot be normalized for browser "
                           "playback. It ships with FFmpeg; note that the imageio-ffmpeg "
                           "package provides ffmpeg only.")
        return None
    stdout = await run_command_async([
        ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(path),
    ])
    if stdout:
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            logger.debug(f"ffprobe returned no valid JSON for {path}.")
    return None




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
    """Reads the given cookie file and returns the contained cookies as dicts."""
    if not cookie_file.exists():
        return []
    return parse_cookies(cookie_file.read_text(encoding="utf-8", errors="replace"))


def parse_cookies(cookies: str) -> list[dict]:
    """Parses cookies exported from a browser into Playwright cookie dicts. Accepts
    both formats offered by the common cookie extensions: the Netscape cookies.txt
    format and JSON."""
    cookies = cookies.strip()
    if not cookies:
        return []
    return _parse_json_cookies(cookies) if cookies[0] in "[{" else _parse_netscape_cookies(cookies)


def _parse_netscape_cookies(cookies: str) -> list[dict]:
    """Parses the Netscape cookies.txt format. Fields are tab-separated:
        domain  include_subdomains  path  is_secure  expiry  name  value
    """
    parsed = []
    for line in cookies.splitlines():
        line = line.strip()
        # Curl and friends mark HttpOnly cookies by prefixing the line
        http_only = line.startswith(HTTP_ONLY_PREFIX)
        if http_only:
            line = line[len(HTTP_ONLY_PREFIX):]
        elif line.startswith("#"):
            continue
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _include_subdomains, path, is_secure, expiry, name, value = parts[:7]
        try:
            expires = int(float(expiry))
        except ValueError:
            continue
        secure = is_secure.upper() == "TRUE"
        parsed.append(_as_cookie(name, value, domain, path, expires, secure, http_only=http_only))
    return parsed


def to_netscape_cookies(cookies: list[dict], header: str = "") -> str:
    """Serializes cookies into the Netscape cookies.txt format, the counterpart of
    `parse_cookies()`. SameSite has no place in that format and is lost; HttpOnly is
    preserved through the '#HttpOnly_' line prefix that curl established."""
    lines = ["# Netscape HTTP Cookie File"]
    lines += [f"# {line}" for line in header.splitlines() if header]
    lines.append("")
    for cookie in cookies:
        domain = cookie.get("domain", "")
        expires = int(cookie.get("expires") or 0)
        line = "\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",  # Include subdomains
            cookie.get("path") or "/",
            "TRUE" if cookie.get("secure") else "FALSE",
            str(max(expires, 0)),  # Playwright marks session cookies with -1
            cookie.get("name", ""),
            cookie.get("value", ""),
        ])
        lines.append(HTTP_ONLY_PREFIX + line if cookie.get("httpOnly") else line)
    return "\n".join(lines) + "\n"


def _parse_json_cookies(cookies: str) -> list[dict]:
    """Parses the JSON format, as exported by extensions like Cookie-Editor."""
    try:
        data = json.loads(cookies)
    except json.JSONDecodeError:
        logger.warning("⚠️ Could not parse the provided cookies: Invalid JSON.")
        return []

    if isinstance(data, dict):  # Some exports wrap the list into an object
        data = data.get("cookies", [])

    parsed = []
    for cookie in data:
        if not isinstance(cookie, dict) or "name" not in cookie or "domain" not in cookie:
            continue
        expires = cookie.get("expires", cookie.get("expirationDate", 0))
        parsed.append(_as_cookie(
            name=cookie["name"],
            value=cookie.get("value", ""),
            domain=cookie["domain"],
            path=cookie.get("path", "/"),
            expires=int(float(expires)) if expires else 0,
            secure=bool(cookie.get("secure")),
            http_only=bool(cookie.get("httpOnly")),
            same_site=cookie.get("sameSite"),
        ))
    return parsed


def _as_cookie(name: str, value: str, domain: str, path: str, expires: int,
               secure: bool, http_only: bool = False, same_site: str | None = None) -> dict:
    """Assembles a Playwright cookie dict, normalizing the SameSite attribute."""
    same_site = SAME_SITE_VALUES.get(str(same_site).lower())
    if same_site is None or (same_site == "None" and not secure):
        # Browsers reject SameSite=None on insecure cookies, so fall back to their
        # default (Lax) whenever the export doesn't tell us better.
        same_site = "Lax"
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path or "/",
        "expires": expires,
        "httpOnly": http_only,
        "secure": secure,
        "sameSite": same_site,
    }


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
