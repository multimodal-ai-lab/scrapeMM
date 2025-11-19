import pytest
from ezmm import MultimodalSequence

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
    print(f"\n{'='*80}")
    print(f"YouTube URL: {url}")
    print(f"Result type: {type(result)}")
    print(f"Success: {result is not None}")
    if result:
        print(f"Has videos: {result.has_videos()}")
        print(f"Length: {len(result)}")
        # Print first 200 chars of text
        text_items = [str(item) for item in result if isinstance(item, str)]
        if text_items:
            print(f"Text preview: {text_items[0][:200]}...")
    print(f"{'='*80}\n")
    assert result is not None
    assert result.has_videos(), "YouTube videos must have video content"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.instagram.com/p/DRJ94KKDhpx/?utm_source=ig_web_copy_link&igsh=NTc4MTIwNjQ2YQ==",
    "https://www.instagram.com/p/DRNcdbPiPCj/?utm_source=ig_web_copy_link&igsh=NTc4MTIwNjQ2YQ==",
    "https://www.instagram.com/reel/DRKtWnhAI0j/?utm_source=ig_web_copy_link",
    "https://www.instagram.com/reel/DRE38jKDIYb/?utm_source=ig_web_copy_link&igsh=MzRlODBiNWFlZA==",
])
async def test_instagram(url):
    """Test Instagram post and reel retrieval"""
    result = await retrieve(url)
    print(f"\n{'='*80}")
    print(f"Instagram URL: {url}")
    print(f"Result type: {type(result)}")
    print(f"Success: {result is not None}")
    if result:
        print(f"Has images: {result.has_images()}")
        print(f"Has videos: {result.has_videos()}")
        print(f"Length: {len(result)}")
        # Count media types
        images = sum(1 for item in result if hasattr(item, 'width'))
        videos = sum(1 for item in result if hasattr(item, 'duration'))
        print(f"Images: {images}, Videos: {videos}")
    print(f"{'='*80}\n")
    assert result is not None

    # Verify media based on URL type
    is_reel = "/reel/" in url
    if is_reel:
        assert result.has_videos(), "Instagram Reels must have video content"
    else:
        assert result.has_images(), "Instagram posts must have images"


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
    print(f"\n{'='*80}")
    print(f"Facebook URL: {url}")
    print(f"Result type: {type(result)}")
    print(f"Success: {result is not None}")
    if result:
        is_photo = "photo" in url
        is_reel = "reel" in url
        print(f"Type: {'Photo' if is_photo else 'Reel' if is_reel else 'Unknown'}")
        print(f"Has images: {result.has_images()}")
        print(f"Has videos: {result.has_videos()}")
        print(f"Length: {len(result)}")
    print(f"{'='*80}\n")
    assert result is not None

    # Verify media based on URL type
    is_photo = "/photo" in url
    is_reel = "/reel/" in url
    if is_reel:
        assert result.has_videos(), "Facebook Reels must have video content"
    elif is_photo:
        assert result.has_images(), "Facebook photos must have images"
