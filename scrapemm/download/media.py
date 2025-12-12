from typing import Optional

import aiohttp
from ezmm import Item

from scrapemm.download.common import HEADERS
from scrapemm.download.images import is_maybe_image_url, download_image
from scrapemm.download.videos import is_maybe_video_url, download_video


async def download_medium(
        url: str,
        session: Optional[aiohttp.ClientSession] = None,
        ignore_small_images: bool = True,
        **kwargs
) -> Optional[Item]:
    """Downloads the item from the given URL and returns an instance of the
    corresponding item class. Reuses a session if provided."""

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(headers=HEADERS)

    try:
        if await is_maybe_image_url(url, session):
            return await download_image(url, ignore_small_images=ignore_small_images, session=session, **kwargs)
        if await is_maybe_video_url(url, session):
            return await download_video(url, session)
        # TODO: Handle audios
    finally:
        if own_session:
            await session.close()
