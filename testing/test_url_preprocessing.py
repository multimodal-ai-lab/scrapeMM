import pytest

from scrapemm.server.util import preprocess_url

pytestmark = pytest.mark.server


@pytest.mark.parametrize("url, expected", [
    # www.malayalam.factcrescendo.com redirects to a host that does not exist
    ("https://www.malayalam.factcrescendo.com/old-image-of-gujarat-flood-shared-as-recent/",
     "https://malayalam.factcrescendo.com/old-image-of-gujarat-flood-shared-as-recent/"),
    ("http://WWW.Malayalam.FactCrescendo.com/a?b=c#d", "http://malayalam.factcrescendo.com/a?b=c#d"),
    ("https://www.malayalam.factcrescendo.com", "https://malayalam.factcrescendo.com"),
    ("  https://www.malayalam.factcrescendo.com/x  ", "https://malayalam.factcrescendo.com/x"),
    # Left as they are
    ("https://malayalam.factcrescendo.com/x", "https://malayalam.factcrescendo.com/x"),
    ("https://cambodia.factcrescendo.com/x", "https://cambodia.factcrescendo.com/x"),
    ("https://www.factcrescendo.com/x", "https://www.factcrescendo.com/x"),
    ("https://www.malayalam.factcrescendo.com.example.org/x",
     "https://www.malayalam.factcrescendo.com.example.org/x"),
    ("https://example.org/?u=https://www.malayalam.factcrescendo.com/x",
     "https://example.org/?u=https://www.malayalam.factcrescendo.com/x"),
])
def test_known_host_fixes(url, expected):
    assert preprocess_url(url) == expected


def test_decoding_and_trimming_are_unchanged():
    assert preprocess_url("  https://example.org/a%20b ") == "https://example.org/a b"


async def test_results_come_back_under_the_url_as_requested(monkeypatch):
    from scrapemm.common import ScrapedContent, ScrapingResponse
    from scrapemm.server import engine
    seen = []

    async def fake(url, session, *args):
        seen.append(url)
        return ScrapingResponse(url=preprocess_url(url), content=ScrapedContent(markdown="x"),
                                method="stub", output_format="markdown")

    monkeypatch.setattr(engine, "_retrieve", fake)
    requested = "https://www.malayalam.factcrescendo.com/some-article/"
    response = await engine.retrieve(requested, show_progress=False, output_format="markdown")
    assert response.url == requested
    assert seen == [requested]
