import asyncio

import aiohttp

from scrapemm.server.download import download_medium, download_image
from scrapemm.server.download.common import HEADERS


async def download(url):
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        return await download_image(url, session)


if __name__ == "__main__":
    result = asyncio.run(download(
        "https://p16-common-sign.tiktokcdn-us.com/tos-useast2a-avt-0068-euttp/7ca6a9e450cc618d8ca90a87e079ad23~tplv-tiktokx-cropcenter:168:168.jpeg?dr=9638&refresh_token=28a5c587&x-expires=1784646000&x-signature=00hQTVGshzNQVVd7V%2Bceb7v9qs8%3D&t=4d5b0474&ps=13740610&shp=a5d48078&shcp=7996dd0b&idc=useast5"))
    print(result)
