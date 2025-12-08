import pytest
from ezmm import MultimodalSequence

from scrapemm import retrieve
from scrapemm.common import ScrapingResponse


def assert_expectations(response: ScrapingResponse, expected: dict[str, int]):
    """Assert that the content has the expected number of images and videos."""
    content = response.content
    print(content)
    assert isinstance(content, MultimodalSequence)
    for medium, count in expected.items():
        match medium:
            case "image":
                assert len(content.images) >= count
            case "video":
                assert len(content.videos) >= count


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=RxJtYsCQ0jo",
    "https://www.youtube.com/watch?v=mUBdMLfkI54",
    "https://www.youtube.com/watch?v=A4dVOznX6Kk",
    "https://www.youtube.com/shorts/1Cgvb17edsQ",
    "https://www.youtube.com/shorts/mM0i832urK0",
    "https://www.youtube.com/shorts/cE0zgN6pYOc",
])
async def test_youtube(url):
    """Test YouTube video and shorts retrieval"""
    result = await retrieve(url)
    assert_expectations(result, dict(video=1))


@pytest.mark.asyncio
@pytest.mark.parametrize("url, expected", [
    ("https://www.instagram.com/p/DRJ94KKDhpx", dict(image=1)),
    ("https://www.instagram.com/p/DRNcdbPiPCj", dict(image=1)),
    ("https://www.instagram.com/p/CqJDbyOP839", dict(image=1)),
    ("https://www.instagram.com/p/DMuOe6th94D", dict(video=1)),  # Yes, this is a video
    ("https://www.instagram.com/reel/DRKtWnhAI0j", dict(video=1)),
    ("https://www.instagram.com/reel/DRE38jKDIYb", dict(video=1)),
    ("https://www.instagram.com/reel/DKqPQqpTDW4", dict(video=1)),
])
async def test_instagram(url: str, expected: dict[str, int]):
    result = await retrieve(url)
    assert_expectations(result, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("url, expected", [
    ("https://www.facebook.com/photo/?fbid=1721085455188778&set=a.107961589834514&_rdc=1&_rdr", dict(image=1)),
    ("https://www.facebook.com/photo/?fbid=1287391456760943&set=a.644291054404323", dict(image=1)),
    ("https://www.facebook.com/photo/?fbid=1148555903931627&set=a.859126372874583", dict(image=1)),
    ("https://www.facebook.com/reel/2038221060315031", dict(video=1)),
    ("https://www.facebook.com/reel/1954696035077530", dict(video=1)),
    ("https://m.facebook.com/watch/?v=567654417277309", dict(video=1)),
    ("https://www.facebook.com/S.Angel000/videos/2081147972022130/", dict(video=1)),
    ("https://fb.watch/dmMvfqIFqC/", dict(video=1)),
    ("https://www.facebook.com/reel/1089214926521000", dict(video=1)),
    ("https://www.facebook.com/reel/3466446073497470", dict(video=1)),  # restricted for misinformation
    ("https://www.facebook.com/61561558177010/videos/1445957793080961/", dict(video=1)),
    ("https://www.facebook.com/watch/?v=1445957793080961", dict(video=1)),
    ("https://www.facebook.com/groups/1973976962823632/posts/3992825270938781/", dict(video=1)),
    # restricted for misinformation, yt-dlp fails here
])
async def test_facebook(url: str, expected: dict[str, int]):
    """Test Facebook reel and photo retrieval"""
    result = await retrieve(url)
    assert_expectations(result, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("url, expected", [
    ("https://t.me/durov/404", dict(image=1)),  # One image
    ("https://t.me/tglobaleye/16172", dict(image=2)),  # Multiple images
    ("https://t.me/tglobaleye/16178", dict(video=1)),  # Video and quote
    ("https://t.me/tglobaleye/6289", dict(video=1)),  # GIF (treated as video)
    ("https://t.me/tglobaleye/16192", dict(image=2, video=1)),  # Images and video
])
async def test_telegram(url: str, expected: dict[str, int]):
    result = await retrieve(url)
    assert_expectations(result, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.tiktok.com/@realdonaldtrump/video/7433870905635409198",
    "https://www.tiktok.com/@xxxx.xxxx5743/video/7521704371109793046"
])
async def test_tiktok(url):
    result = await retrieve(url)
    assert_expectations(result, dict(video=1))


@pytest.mark.asyncio
@pytest.mark.parametrize("url, expected", [
    ("https://x.com/PopBase/status/1938496291908030484", dict(image=1)),
    ("https://x.com/realDonaldTrump", dict())
])
async def test_x(url: str, expected: dict[str, int]):
    result = await retrieve(url)
    assert_expectations(result, expected)
