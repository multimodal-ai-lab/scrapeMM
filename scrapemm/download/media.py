from typing import Optional, Union, TYPE_CHECKING

import aiohttp
from ezmm import Item

if TYPE_CHECKING:
    from playwright.async_api import APIRequestContext

from scrapemm.download.common import HEADERS
from scrapemm.download.images import is_maybe_image_url, download_image
from scrapemm.download.videos import is_maybe_video_url, download_video


async def download_medium(
        url: str,
        session: Optional[Union[aiohttp.ClientSession, "APIRequestContext"]] = None,
        ignore_small_images: bool = True,
        **kwargs
) -> Optional[Item]:
    """Downloads the item from the given URL and returns an instance of the
    corresponding item class. Reuses a session if provided."""

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(headers=HEADERS)

    assert session is not None
    try:
        # If content type is octet-stream, we don't know if it's an image or a video.
        # So we first attempt to download it as an image, and if that fails, we attempt to download it as a video. 
        if await is_maybe_image_url(url, session):
            image = await download_image(url, ignore_small_images=ignore_small_images, session=session, **kwargs)
            if image is not None:
                return image
        if await is_maybe_video_url(url, session):
            return await download_video(url, session=session, **kwargs)
    except Exception:
        pass
    finally:
        if own_session:
            await session.close()
