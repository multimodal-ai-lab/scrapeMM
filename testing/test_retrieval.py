import pytest

from scrapemm import retrieve



@pytest.mark.asyncio
async def test_generic_retrieval():
    urls = [
        "https://www.zeit.de/politik/deutschland/2025-07/spionage-iran-festnahme-anschlag-juden-berlin-daenemark",
        "https://factnameh.com/fa/fact-checks/2025-04-16-araghchi-witkoff-fake-photo",
        "https://www.thip.media/health-news-fact-check/fact-check-can-a-kalava-on-the-wrist-prevent-paralysis/74724/",
        "https://www.vishvasnews.com/viral/fact-check-upsc-has-not-reduced-the-maximum-age-limit-for-ias-and-ips-exams/",
        "https://www.thip.media/health-news-fact-check/fact-check-does-wrapping-body-with-banana-leaves-help-with-obesity-and-indigestion/71333/",
        "https://health.medicaldialogues.in/fact-check/brain-health-fact-check/fact-check-is-sprite-the-best-remedy-for-headaches-in-the-world-140368",
        "https://www.washingtonpost.com/politics/2024/05/15/bidens-false-claim-that-inflation-was-9-percent-when-he-took-office/",
        "https://assamese.factcrescendo.com/viral-claim-that-the-video-shows-the-incident-from-uttar-pradesh-and-the-youth-on-the-bike-and-the-youth-being-beaten-and-taken-away-by-the-police-are-the-same-youth-named-abdul-is-false/",
        "https://factuel.afp.com/doc.afp.com.43ZN7NP",
        "https://x.com/PopBase/status/1938496291908030484",
    ]
    results = await retrieve(urls)
    assert len(results) == len(urls)
    assert all(results)


@pytest.mark.asyncio
async def test_telegram_retrieval():
    urls = [
        "https://t.me/durov/404",  # One image
        "https://t.me/tglobaleye/16172",  # Multiple images
        "https://t.me/tglobaleye/16178",  # Video and quote
        "https://t.me/tglobaleye/6289",  # GIF (treated as video)
        "https://t.me/tglobaleye/16192",  # Images and video
    ]
    results = await retrieve(urls)
    print(results)
    assert len(results) == len(urls)
    assert all(results)


@pytest.mark.asyncio
async def test_tiktok_retrieval():
    urls = [
        "https://www.tiktok.com/@realdonaldtrump/video/7433870905635409198",
        "https://www.tiktok.com/@xxxx.xxxx5743/video/7521704371109793046"
    ]
    results = await retrieve(urls)
    print(results)
    assert len(results) == len(urls)
    assert all(results)
