import asyncio

import aiohttp

from scrapemm.download import download_medium
from scrapemm.download.common import HEADERS


async def download(url):
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        return await download_medium(url, session)


if __name__ == "__main__":
    asyncio.run(download(
        "https://media.cnn.com/api/v1/images/stellar/prod/ap22087057359494.jpg?c=16x9&q=h_653,w_1160,c_fill/f_webp"))
