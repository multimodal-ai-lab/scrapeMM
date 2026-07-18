from io import BytesIO
from typing import Optional, Union, TYPE_CHECKING

import PIL
import aiohttp
from PIL.Image import Resampling
from PIL import ImageFile
from ezmm import Image

from scrapemm.download.util import looks_like_image_file_url

if TYPE_CHECKING:
    from playwright.async_api import APIRequestContext

from scrapemm.download.requests import request_static, fetch_headers

# Tolerate images that are truncated
ImageFile.LOAD_TRUNCATED_IMAGES = True


async def download_image(
        image_url: str,
        session: Union[aiohttp.ClientSession, "APIRequestContext"],
        ignore_small_images: bool = True,
        max_size: tuple[int, int] = (2048, 2048),
        **kwargs
) -> Optional[Image]:
    """Download an image from a URL and return it as an Image object."""
    # TODO: Handle very large images like: https://eoimages.gsfc.nasa.gov/images/imagerecords/144000/144225/campfire_oli_2018312_lrg.jpg
    content = await request_static(image_url, session, get_text=False, **kwargs)
    if content:
        assert isinstance(content, bytes)
        return image_from_binary(content, image_url, ignore_small_images=ignore_small_images, max_size=max_size)


def image_from_binary(
        content: bytes,
        source_url: str,
        ignore_small_images: bool = True,
        max_size: tuple[int, int] = (2048, 2048)
) -> Optional[Image]:
    """Returns an Image object from a binary image content."""
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
            return image


async def is_maybe_image_url(url: str, session: Union[aiohttp.ClientSession, "APIRequestContext"]) -> bool:
    """Returns True iff the URL points at an accessible _pixel_ image file
    or if the content type is a binary download stream."""
    try:
        headers = await fetch_headers(url, session, timeout=3000, allow_redirects=True)
        content_type = headers.get('Content-Type') or headers.get('content-type')
        if content_type.startswith("image/"):
            # Surely an image
            return (not "svg" in content_type and
                    not "eps" in content_type)
        else:
            # If the content is a binary download stream, it likely encodes an image
            # if also the URL looks like an image file URL.
            return content_type == "binary/octet-stream" and looks_like_image_file_url(url)

    except Exception:
        return False
