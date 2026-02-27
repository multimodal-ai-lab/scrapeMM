import pytest

from scrapemm import retrieve
from test_social_media import assert_expectations


@pytest.mark.asyncio
@pytest.mark.parametrize("url, expected", [
    # Perma.cc:
    ("https://perma.cc/N8MR-NS96", dict(video=1)),  # TikTok video
    ("https://perma.cc/HU7Z-B24D", dict(image=1)),  # Screenshot of Twitter post
    ("https://perma.cc/HW47-P32Z", dict(image=1)),  # Screenshot of Instagram post
    ("https://perma.cc/8J8J-2CEB", dict(image=4)),  # 4 images in old Twitter UI
    ("https://perma.cc/38CZ-MMFV", dict(image=1)),  # Web article with one image
    ("https://perma.cc/ZD7Z-B3U7?type=image", dict(image=1)),  # Screenshot of Facebook video
    ("https://perma.cc/D5GB-S4E9", dict(image=1)),  # Recorded screenshot of Facebook post
    # Archive.today:
    ("https://archive.is/uTVE4", dict(image=1)),  # X post
    ("https://archive.is/0VrgI", dict(image=1)),  # Facebook post
    ("http://archive.today/6OttS", dict(image=2)),  # Facebook post
    ("http://archive.today/2022.05.05-091515/https:/twitter.com/SamvanRooy1/status/1521438261130014721", dict(image=2)),  # Twitter post
    ("https://archive.ph/movd4", dict(image=1)),  # Online article
    ("https://archive.vn/Edqcv", dict(image=1)),  # Online article
    ("https://archive.fo/jnN0O", dict(image=1)),  # Older Facebook post
    # ("https://mvau.lt/media/b286c959-00da-4765-8f49-88d4ca87a555", dict(video=1)),
    # ("https://mvau.lt/media/ccfa5e89-a89d-4a12-aee1-cf68dcb205ce", dict(image=1)),
    # ("https://web.archive.org/web/20210604181412/https://www.tiktok.com/@realstewpeters/video/6969789589590379781?is_copy_url=1", dict(video=1)),
    # ("http://web.archive.org/web/20230308184253/https://newspunch.com/hungary-prepares-to-prosecute-lifelong-nazi-george-soros-for-holocaust-atrocities/", dict(video=3, image=1)),
    # ("https://ghostarchive.org/archive/d63Zd", dict(image=1)),
    # ("https://ghostarchive.org/archive/gz80n", dict(video=1)),
    # ("https://www.awesomescreenshot.com/image/40260716?key=c50159e80af3be1d003aa5235d3edaa9", dict(image=1)),
    # ("https://www.awesomescreenshot.com/video/11862930?key=b021c372bb96716fdbb2316d0eb37c69", dict(video=1))
])
async def test_archiving_service(url: str, expected: dict[str, int]):
    result = await retrieve(url)
    assert_expectations(result, expected)
