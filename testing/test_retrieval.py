import pytest
from ezmm import MultimodalSequence

from scrapemm import retrieve


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.vishvasnews.com/viral/fact-check-upsc-has-not-reduced-the-maximum-age-limit-for-ias-and-ips-exams/",
    "https://health.medicaldialogues.in/fact-check/brain-health-fact-check/fact-check-is-sprite-the-best-remedy-for-headaches-in-the-world-140368",
    "https://www.washingtonpost.com/politics/2024/05/15/bidens-false-claim-that-inflation-was-9-percent-when-he-took-office/",
    "https://assamese.factcrescendo.com/viral-claim-that-the-video-shows-the-incident-from-uttar-pradesh-and-the-youth-on-the-bike-and-the-youth-being-beaten-and-taken-away-by-the-police-are-the-same-youth-named-abdul-is-false/",
    "https://factuel.afp.com/doc.afp.com.43ZN7NP",
    "https://leadstories.com/365cb414b83e29d26fecae374d55c743a3eac4c7.png",
    "https://leadstories.com/assets_c/2025/08/193f14f06dd6f15b89bf8050e553ad7fb1be6530-thumb-900xauto-3165872.png"
])
@pytest.mark.parametrize("method", ["firecrawl", "decodo"])
async def test_generic_retrieval(url, method):
    result = await retrieve(url, methods=[method])
    print(result)
    assert result
    assert isinstance(result, MultimodalSequence)
    assert result.has_images()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.zeit.de/politik/deutschland/2025-07/spionage-iran-festnahme-anschlag-juden-berlin-daenemark",
    "https://factnameh.com/fa/fact-checks/2025-04-16-araghchi-witkoff-fake-photo",
    "https://www.thip.media/health-news-fact-check/fact-check-can-a-kalava-on-the-wrist-prevent-paralysis/74724/",
])
@pytest.mark.parametrize("method", ["firecrawl", "decodo"])
async def test_html_retrieval(url, method):
    result = await retrieve(url, format="html", methods=[method])
    print(result)
    assert result
    assert isinstance(result, str)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://t.me/durov/404",  # One image
    "https://t.me/tglobaleye/16172",  # Multiple images
    "https://t.me/tglobaleye/16178",  # Video and quote
    "https://t.me/tglobaleye/6289",  # GIF (treated as video)
    "https://t.me/tglobaleye/16192",  # Images and video
])
async def test_telegram(url):
    result = await retrieve(url)
    print(result)
    assert result


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.tiktok.com/@realdonaldtrump/video/7433870905635409198",
    "https://www.tiktok.com/@xxxx.xxxx5743/video/7521704371109793046"
])
async def test_tiktok(url):
    result = await retrieve(url)
    print(result)
    assert result


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://x.com/PopBase/status/1938496291908030484",
    "https://x.com/realDonaldTrump"
])
async def test_x(url):
    result = await retrieve(url)
    print(result)
    assert result


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.instagram.com/p/CqJDbyOP839",
])
async def test_instagram_images(url):
    result = await retrieve(url)
    print(result)
    assert result
    assert result.has_images()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.instagram.com/reel/DKqPQqpTDW4",
    "https://www.instagram.com/reel/C75nh7Lvo8F",
    "https://www.instagram.com/p/DMuOe6th94D",  # yes, this is a video
])
async def test_instagram_videos(url):
    result = await retrieve(url)
    print(result)
    assert result
    assert result.has_videos()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.facebook.com/reel/1089214926521000",
    "https://www.facebook.com/reel/3466446073497470",  # restricted for misinformation
    "https://www.facebook.com/61561558177010/videos/1445957793080961/",
    "https://www.facebook.com/watch/?v=1445957793080961",
    "https://www.facebook.com/groups/1973976962823632/posts/3992825270938781/",  # restricted for misinformation, yt-dlp fails here
])
async def test_facebook_videos(url):
    result = await retrieve(url)
    print(result)
    assert result
    assert result.has_videos()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.facebook.com/photo/?fbid=1721085455188778&set=a.107961589834514&_rdc=1&_rdr",
])
async def test_facebook_images(url):
    result = await retrieve(url)
    print(result)
    assert result
    assert result.has_images()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=A4dVOznX6Kk",
    "https://www.youtube.com/shorts/cE0zgN6pYOc",
])
async def test_youtube(url):
    result = await retrieve(url)
    print(result)
    assert result
    assert result.has_videos()


@pytest.mark.asyncio
@pytest.mark.parametrize("url, max_video_size, download_expected", [
    # ("https://www.facebook.com/reel/1089214926521000", None, True),
    # ("https://www.facebook.com/reel/1089214926521000", 128_000_000, True),
    ("https://www.facebook.com/reel/1089214926521000", 1_000_000, False),
    # ("https://www.youtube.com/shorts/cE0zgN6pYOc", None, True),
    # ("https://www.youtube.com/shorts/cE0zgN6pYOc", 4_000_000, True),
    # ("https://www.youtube.com/shorts/cE0zgN6pYOc", 3_000_000, False),
])
async def test_max_video_size(url, max_video_size, download_expected):
    result = await retrieve(url, max_video_size=max_video_size)
    assert result.has_videos() == download_expected
    if max_video_size and result.has_videos():
        video = result.videos[0]
        assert video.size <= max_video_size
