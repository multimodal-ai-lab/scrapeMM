import aiohttp
import pytest
from ezmm import Image, Item, Video

from scrapemm.download import download_medium, download_image, download_video
from scrapemm.download.common import HEADERS
from scrapemm.download.images import is_maybe_image_url


@pytest.mark.parametrize("url,expected", [
    ("https://media.cnn.com/api/v1/images/stellar/prod/ap22087057359494.jpg?c=16x9&q=h_653,w_1160,c_fill", True),
    ("https://edition.cnn.com/2024/10/30/asia/north-korea-icbm-test-intl-hnk/index.html", False),
    ("https://img.zeit.de/politik/ausland/2024-10/georgien-wahl-stimmauszaehlung-regierungspartei-bild/wide__1000x562__desktop__scale_2",
     True),
    ("https://upload.wikimedia.org/wikipedia/commons/8/8d/President_Barack_Obama.jpg", True),
    ("https://de.wikipedia.org/wiki/Datei:President_Barack_Obama.jpg", False),  # This is the image's article view
    ("https://bingekulture.com/wp-content/uploads/2021/08/cropped-cropped-logo.fw-removebg-preview.png?w=48", False),
    # this URL redirects to a webpage
    ("https://www.popularmechanics.com/_assets/design-tokens/fre/static/icons/play.db7c035.svg?primary=%2523ffffff%20%22Play%22",
     False),  # this is a vector graphic
    ("https://pixum-cms.imgix.net/7wL8j3wldZEONCSZB9Up6B/d033b7b6280687ce2e4dfe2d4147ff93/fab_mix_kv_perspektive_foto_liegend_desktop__3_.png?auto=compress,format&trim=false&w=2000",
     True),
    ("https://cdn.pixabay.com/photo/2017/11/08/22/28/camera-2931883_1280.jpg", True),
    # image is presented as a binary download stream
    ("https://arxiv.org/pdf/2412.10510", False),  # this is a PDF download stream
    ("https://platform.vox.com/wp-content/uploads/sites/2/2025/04/jack-black-wink-minecraft.avif?quality=90&strip=all&crop=12.5%2C0%2C75%2C100&w=2400",
     True),
    ("https://media.cnn.com/api/v1/images/stellar/prod/02-overview-of-kursk-training-area-15april2025-wv2.jpg?q=w_1110,c_fill",
     True)
])
@pytest.mark.asyncio
async def test_is_image_url(url, expected):
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        assert await is_maybe_image_url(url, session) == expected


async def download_img(url):
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        return await download_image(url, session)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://media.cnn.com/api/v1/images/stellar/prod/02-overview-of-kursk-training-area-15april2025-wv2.jpg?q=w_1110,c_fill",
    "https://factly.in/wp-content/uploads/2025/02/Train-fire-in-Prayagraj-Claim.jpg",
    "https://www.washingtonpost.com/wp-apps/imrs.php?src=https://arc-anglerfish-washpost-prod-washpost.s3.amazonaws.com/public/MBWA4LJ5XLVC6CJLZG2OQFMGWE.JPG&w=1440&impolicy=high_res",
    "https://factuel.afp.com/sites/default/files/styles/header_article/public/medias/factchecking/g2/2025-07/c1452a5562cfe3e178b0d5c6681c940e-fr.jpeg?itok=a9Wc3hEY",
    "https://archive.is/uTVE4/da2c6541801809f1b665e8992f7d214621ec9443/scr.png",
])
async def test_download_image(url):
    img = await download_img(url)
    print(img)
    assert isinstance(img, Image)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://media.cnn.com/api/v1/images/stellar/prod/ap22087057359494.jpg?c=16x9&q=h_653,w_1160,c_fill/f_webp"
])
async def test_download_item(url):
    item = await download_medium(url)
    assert isinstance(item, Item)
    print(item)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://demo.unified-streaming.com/k8s/features/stable/video/tears-of-steel/tears-of-steel.ism/.m3u8",
    "https://devstreaming-cdn.apple.com/videos/streaming/examples/adv_dv_atmos/main.m3u8",
    "https://video.bsky.app/watch/did%3Aplc%3Alvs2rrkrj6usatuglfukwoea/bafkreibdgmt4y3z62opupxdykw53ftvkyoprzxuztzocxqfe2hjskziq44/playlist.m3u8",
])
async def test_download_m3u8(url):
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        vid = await download_video(url, session)
        assert isinstance(vid, Video)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://file-examples.com/storage/fef2d60de7693b530968f16/2017/04/file_example_MP4_480_1_5MG.mp4",
    "https://file-examples.com/storage/fef2d60de7693b530968f16/2020/03/file_example_WEBM_480_900KB.webm",
])
async def test_download_mp4_webm(url):
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        vid = await download_video(url, session)
        assert isinstance(vid, Video)
