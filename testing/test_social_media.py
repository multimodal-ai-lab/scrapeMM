import pytest
from ezmm import MultimodalSequence
from scrapemm.common import ScrapingResponse

from scrapemm import retrieve


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=RxJtYsCQ0jo",
    "https://www.youtube.com/watch?v=mUBdMLfkI54",
    "https://www.youtube.com/shorts/1Cgvb17edsQ?feature=share",
    "https://www.youtube.com/shorts/mM0i832urK0?feature=share",
])
async def test_youtube(url):
    """Test YouTube video and shorts retrieval"""
    result = await retrieve(url)
    assert isinstance(result, ScrapingResponse)
    content = result.content
    assert isinstance(content, MultimodalSequence)
    assert content.has_videos()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.instagram.com/p/DRJ94KKDhpx",
    "https://www.instagram.com/p/DRNcdbPiPCj",
    "https://www.instagram.com/reel/DRKtWnhAI0j",
    "https://www.instagram.com/reel/DRE38jKDIYb",
])
async def test_instagram(url):
    """Test Instagram post and reel retrieval"""
    result = await retrieve(url)
    assert isinstance(result, ScrapingResponse)
    content = result.content
    assert isinstance(content, MultimodalSequence)

    if "reel" in url:
        assert content.has_videos()
    else:
        assert content.has_images()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.facebook.com/reel/2038221060315031",
    "https://www.facebook.com/photo/?fbid=1287391456760943&set=a.644291054404323",
    "https://www.facebook.com/photo/?fbid=1148555903931627&set=a.859126372874583",
    "https://www.facebook.com/reel/1954696035077530",
])
async def test_facebook(url):
    """Test Facebook reel and photo retrieval"""
    result = await retrieve(url)
    assert isinstance(result, ScrapingResponse)
    content = result.content
    assert isinstance(content, MultimodalSequence)

    if "photo" in url:
        assert content.has_images()
    elif "reel" in url:
        assert content.has_videos()
