from scrapemm import retrieve
import pytest

@pytest.mark.asyncio
@pytest.mark.parametrize("url, expected", [
    ("https://mvau.lt/media/b286c959-00da-4765-8f49-88d4ca87a555", dict(video=1)),
    ("https://mvau.lt/media/ccfa5e89-a89d-4a12-aee1-cf68dcb205ce", dict(image=1)),
    ("https://web.archive.org/web/20210604181412/https://www.tiktok.com/@realstewpeters/video/6969789589590379781?is_copy_url=1", dict(video=1)),
    ("http://web.archive.org/web/20230308184253/https://newspunch.com/hungary-prepares-to-prosecute-lifelong-nazi-george-soros-for-holocaust-atrocities/", dict(video=3, image=1)),
    ("https://perma.cc/N8MR-NS96", dict(video=1)),
    ("https://perma.cc/D5GB-S4E9", dict(image=1)),
    ("https://ghostarchive.org/archive/d63Zd", dict(image=1)),
    ("https://ghostarchive.org/archive/gz80n", dict(video=1)),
    ("https://archive.is/uTVE4", dict(image=1)),
    ("https://archive.is/0VrgI", dict(image=3)),
    ("https://www.awesomescreenshot.com/image/40260716?key=c50159e80af3be1d003aa5235d3edaa9", dict(image=1)),
    ("https://www.awesomescreenshot.com/video/11862930?key=b021c372bb96716fdbb2316d0eb37c69", dict(video=1))
])
async def test_archiving_service(url: str, expected: dict[str, int]):
    result = await retrieve(url)
    content = result.content
    print(content)
    assert content
    for medium, count in expected.items():
        match medium:
            case "image": assert len(content.images) >= count
            case "video": assert len(content.videos) >= count
