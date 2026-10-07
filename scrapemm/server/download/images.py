import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import Optional, Union, TYPE_CHECKING

import PIL
import aiohttp
from PIL.Image import Resampling
from PIL import ImageFile
from ezmm import Image

from scrapemm.server.download.util import looks_like_image_file_url

if TYPE_CHECKING:
    from playwright.async_api import APIRequestContext

from scrapemm.server.download.requests import request_static, fetch_headers

logger = logging.getLogger("scrapeMM")

# Tolerate images that are truncated
ImageFile.LOAD_TRUNCATED_IMAGES = True

# Threads decoding and rescaling images. Bounded: every busy Python thread competes with
# the event loop for the GIL, and a page with hundreds of images otherwise put a dozen
# of them to work at once, stalling the loop -- and so every retrieval -- for up to a
# second at a time.
_image_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="images")


async def decode_image(content: bytes, source_url: str, **kwargs) -> Optional[Image]:
    """`image_from_binary()` in the image threads, off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(
        _image_executor, lambda: image_from_binary(content, source_url, **kwargs))


async def download_image(
        image_url: str,
        session: Union[aiohttp.ClientSession, "APIRequestContext"],
        ignore_small_images: bool = True,
        max_size: tuple[int, int] = (2048, 2048),
        **kwargs
) -> Optional[Image]:
    """Download an image from a URL and return it as an Image object. An archived image
    comes from the scrapeMM cache, as the registry item it became before (see
    `ImmutableCache.item()`)."""
    from scrapemm.server.cache import immutable
    variant = image_variant(max_size)
    hit, image = await immutable.item(image_url, variant=variant, ignore_small=ignore_small_images)
    if hit:
        return image
    # TODO: Handle very large images like: https://eoimages.gsfc.nasa.gov/images/imagerecords/144000/144225/campfire_oli_2018312_lrg.jpg
    content = await request_static(image_url, session, get_text=False, **kwargs)
    if content:
        assert isinstance(content, bytes)
        # Decoding and rescaling are CPU-bound and would otherwise stall every other
        # retrieval running on this event loop.
        image = await decode_image(content, image_url,
                                   ignore_small_images=ignore_small_images, max_size=max_size)
        await immutable.keep_item(image_url, image, variant=variant)
        return image


def image_variant(max_size: Optional[tuple[int, int]]) -> str:
    """How an image's item depends on its size limit, for the cache (see `download_image()`)."""
    return "" if max_size in (None, (2048, 2048)) else f"#max={max_size[0]}x{max_size[1]}"


def image_from_binary(
        content: bytes,
        source_url: str,
        ignore_small_images: bool = True,
        max_size: tuple[int, int] = (2048, 2048)
) -> Optional[Image]:
    """Returns an Image object from a binary image content. An SVG is rasterized first."""
    if _is_svg(content):
        content = rasterize_svg(content, max_size, ignore_small_images)
        if content is None:
            return None
    try:
        pillow_img = PIL.Image.open(BytesIO(content))
    except PIL.UnidentifiedImageError:
        return None

    if pillow_img:
        if pillow_img.width > max_size[0] or pillow_img.height > max_size[1]:
            pillow_img.thumbnail(max_size, Resampling.LANCZOS)  # Preserves aspect ratio

        if not ignore_small_images or (pillow_img.width > 256 and pillow_img.height > 256):
            image = Image(pillow_image=pillow_img, source_url=source_url)
            image.relocate(move_not_copy=True)  # Ensure the image is in the temp dir + follows simple naming
            # The pixels are on disk now and reload lazily. Kept, they add up to GBs over
            # a batch; the size is all the retrieval still needs (see `image_size()`).
            image._scrapemm_size = (pillow_img.width, pillow_img.height)
            image._image = None
            return image


# An SVG's declared size: the root element's width and height, or else its viewBox
_SVG_ROOT = re.compile(rb"<svg\b[^>]*>", re.IGNORECASE)
_SVG_LENGTH = re.compile(rb"""(?<![-\w])(width|height)\s*=\s*["']\s*([0-9.]+)\s*(?:px)?\s*["']""", re.IGNORECASE)
_SVG_VIEWBOX = re.compile(rb"""\bviewBox\s*=\s*["']\s*[-0-9.]+[\s,]+[-0-9.]+[\s,]+([0-9.]+)[\s,]+([0-9.]+)""", re.IGNORECASE)


def _is_svg(content: bytes) -> bool:
    head = content[:2048].lstrip()
    return head.startswith((b"<svg", b"<?xml", b"<!DOCTYPE svg", b"<!--")) and b"<svg" in head.lower()


def _svg_size(content: bytes) -> Optional[tuple[float, float]]:
    """The size the SVG declares for itself, in pixels, if it says."""
    root = _SVG_ROOT.search(content[:16384])
    if root is None:
        return None
    lengths = {m.group(1).lower(): float(m.group(2)) for m in _SVG_LENGTH.finditer(root.group(0))}
    if b"width" in lengths and b"height" in lengths:
        return lengths[b"width"], lengths[b"height"]
    if box := _SVG_VIEWBOX.search(root.group(0)):
        return float(box.group(1)), float(box.group(2))
    return None


def rasterize_svg(content: bytes, max_size: tuple[int, int] = (2048, 2048),
                  ignore_small_images: bool = True) -> Optional[bytes]:
    """The SVG as a PNG on white, at its own size but within `max_size`. None for one
    that declares itself small (an icon or a logo: not rendered at all) or fails to render.

    SVGs used to be skipped altogether, as icons. But charts and diagrams are SVGs too,
    and for many a page they are the content (e.g. statistics of a government office).
    resvg renders without scripts and without fetching anything."""
    import resvg_py
    size = _svg_size(content)
    if size and ignore_small_images and min(size) <= 256:
        return None
    options = {}
    if size and (size[0] > max_size[0] or size[1] > max_size[1]):
        # Rendered at the limit straight away: a huge declared canvas would cost GBs
        scale = min(max_size[0] / size[0], max_size[1] / size[1])
        options = {"width": max(1, int(size[0] * scale)), "height": max(1, int(size[1] * scale))}
    elif not size:
        options = {"width": max_size[0]}  # No size of its own: rendered as large as allowed
    try:
        return bytes(resvg_py.svg_to_bytes(svg_string=content.decode("utf-8", errors="replace"),
                                           background="#ffffff", **options))
    except ValueError:
        logger.debug("Could not render an SVG.", exc_info=True)
        return None


def image_size(image: Image) -> tuple[int, int]:
    """The image's size, without loading its pixels if `image_from_binary()` made it."""
    return getattr(image, "_scrapemm_size", None) or (image.width, image.height)


async def is_maybe_image_url(url: str, session: Union[aiohttp.ClientSession, "APIRequestContext"]) -> bool:
    """Returns True iff the URL points at an accessible _pixel_ image file
    or if the content type is a binary download stream."""
    try:
        headers = await fetch_headers(url, session, allow_redirects=True)
        content_type = headers.get('Content-Type') or headers.get('content-type') or ''
        if content_type.startswith("image/"):
            # Surely an image. SVGs are rasterized (see `rasterize_svg()`), EPS is not
            return "eps" not in content_type
        else:
            # If the content is a binary download stream, it likely encodes an image
            # if also the URL looks like an image file URL.
            return content_type == "binary/octet-stream" and looks_like_image_file_url(url)

    except Exception:
        logger.debug(f"Error probing image URL {url}", exc_info=True)
        return False
