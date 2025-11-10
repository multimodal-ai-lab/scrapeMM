import pytest

from scrapemm import retrieve


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.zeit.de/politik/deutschland/2025-07/spionage-iran-festnahme-anschlag-juden-berlin-daenemark",
    "https://factnameh.com/fa/fact-checks/2025-04-16-araghchi-witkoff-fake-photo",
    "https://www.thip.media/health-news-fact-check/fact-check-can-a-kalava-on-the-wrist-prevent-paralysis/74724/",
    "https://www.vishvasnews.com/viral/fact-check-upsc-has-not-reduced-the-maximum-age-limit-for-ias-and-ips-exams/",
    "https://www.thip.media/health-news-fact-check/fact-check-does-wrapping-body-with-banana-leaves-help-with-obesity-and-indigestion/71333/",
    "https://health.medicaldialogues.in/fact-check/brain-health-fact-check/fact-check-is-sprite-the-best-remedy-for-headaches-in-the-world-140368",
    "https://www.washingtonpost.com/politics/2024/05/15/bidens-false-claim-that-inflation-was-9-percent-when-he-took-office/",
    "https://assamese.factcrescendo.com/viral-claim-that-the-video-shows-the-incident-from-uttar-pradesh-and-the-youth-on-the-bike-and-the-youth-being-beaten-and-taken-away-by-the-police-are-the-same-youth-named-abdul-is-false/",
    "https://factuel.afp.com/doc.afp.com.43ZN7NP",
    "https://leadstories.com/365cb414b83e29d26fecae374d55c743a3eac4c7.png",
    "https://leadstories.com/Screenshot%202025-10-01%20061351.png"
])
async def test_generic_retrieval(url):
    result = await retrieve(url)
    print(result)
    assert result


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://t.me/durov/404",  # One image
    "https://t.me/tglobaleye/16172",  # Multiple images
    "https://t.me/tglobaleye/16178",  # Video and quote
    "https://t.me/tglobaleye/6289",  # GIF (treated as video)
    "https://t.me/tglobaleye/16192",  # Images and video
])
async def test_telegram_retrieval(url):
    result = await retrieve(url)
    print(result)
    assert result


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.tiktok.com/@realdonaldtrump/video/7433870905635409198",
    "https://www.tiktok.com/@xxxx.xxxx5743/video/7521704371109793046"
])
async def test_tiktok_retrieval(url):
    result = await retrieve(url)
    print(result)
    assert result


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://x.com/PopBase/status/1938496291908030484",
    "https://x.com/realDonaldTrump"
])
async def test_x_retrieval(url):
    result = await retrieve(url)
    print(result)
    assert result
