import pytest

from retrieval import retrieve

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
    "https://i.ytimg.com/vi/4OhACDLcOoY/hqdefault.jpg?sqp=-oaymwEmCKgBEF5IWvKriqkDGQgBFQAAiEIYAdgBAeIBCggYEAIYBjgBQAE=&rs=AOn4CLCXFo0dMvTXmBS0CjzhG4SbckkomQ",
    "https://x.com/PopBase/status/1938496291908030484",
    "https://t.me/durov/404"
]

@pytest.mark.asyncio
async def test_retrieval():
    results = await retrieve(urls)
    assert len(results) == len(urls)
    assert all(results)
