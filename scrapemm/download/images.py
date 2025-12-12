from io import BytesIO
from typing import Optional

import PIL
import aiohttp
from PIL.Image import Resampling
from ezmm import Image

from scrapemm.download.requests import request_static, fetch_headers


async def download_image(
        image_url: str,
        session: aiohttp.ClientSession,
        ignore_small_images: bool = True,
        max_size: tuple[int, int] = (2048, 2048),
        **kwargs
) -> Optional[Image]:
    """Download an image from a URL and return it as an Image object."""
    # TODO: Handle very large images like: https://eoimages.gsfc.nasa.gov/images/imagerecords/144000/144225/campfire_oli_2018312_lrg.jpg
    content = await request_static(image_url, session, get_text=False, **kwargs)
    # TODO: Request page dynamically (better move all request-related functions to ScrapeMM)
    if content:
        try:
            pillow_img = PIL.Image.open(BytesIO(content))
        except PIL.UnidentifiedImageError:
            return None

        if pillow_img:
            if pillow_img.width > max_size[0] or pillow_img.height > max_size[1]:
                pillow_img.thumbnail(max_size, Resampling.LANCZOS)  # Preserves aspect ratio

            if not ignore_small_images or (pillow_img.width > 256 and pillow_img.height > 256):
                image = Image(pillow_image=pillow_img, source_url=image_url)
                image.relocate(move_not_copy=True)  # Ensure the image is in the temp dir + follows simple naming
                return image


async def is_maybe_image_url(url: str, session: aiohttp.ClientSession) -> bool:
    """Returns True iff the URL points at an accessible _pixel_ image file
    or if the content type is a binary download stream."""
    try:
        headers = await fetch_headers(url, session, timeout=3, allow_redirects=True)
        content_type = headers.get('Content-Type') or headers.get('content-type')
        if content_type.startswith("image/"):
            # Surely an image
            return (not "svg" in content_type and
                    not "eps" in content_type)
        else:
            # If the content is a binary download stream, it may encode an image
            # but also something else. This is a case of "maybe an image"
            return content_type == "binary/octet-stream"

    except Exception:
        return False
