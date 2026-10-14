"""Archived content beyond the replay: Bloomberg articles from their page data, X posts
from the archive's record of the X API, and the live post where a capture holds none."""

import json

import pytest

from scrapemm.common.scraping_response import Capture, ScrapedContent, ScrapingResponse
from scrapemm.server.integrations.bloomberg import bloomberg_article_html
from scrapemm.server.integrations.wayback_x import X_POST

pytestmark = pytest.mark.server

STORY = {"props": {"pageProps": {"story": {
    "headline": "Merz Wants a Fast Start", "byline": "Jane Doe", "publishedAt": "2025-05-06T05:00:00Z",
    "body": {"content": [
        {"type": "paragraph", "content": [{"type": "text", "value": "Merz is taking charge "},
                                          {"type": "entity", "data": {"link": {"destination": {"web": "https://x.test/a"}}},
                                           "content": [{"type": "text", "value": "at a critical moment"}]}]},
        {"type": "ad"},
        {"type": "heading", "data": {"level": 2}, "content": [{"type": "text", "value": "Next steps"}]},
    ]}}}}}


def test_a_bloomberg_article_from_its_page_data():
    markup = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(STORY)}</script></html>'
    article = bloomberg_article_html(markup)
    assert "<h1>Merz Wants a Fast Start</h1>" in article and "By Jane Doe" in article
    assert '<p>Merz is taking charge <a href="https://x.test/a">at a critical moment</a></p>' in article
    assert "<h2>Next steps</h2>" in article
    assert bloomberg_article_html("<html>No data</html>") is None


def test_x_post_urls():
    assert X_POST.match("https://x.com/vpopulimedia/status/2013656465060302945")
    assert X_POST.match("https://twitter.com/a_b/status/1/photo/1")
    assert not X_POST.match("https://x.com/vpopulimedia")


def test_the_live_post_says_so_and_is_not_kept_for_good():
    from scrapemm.server.cache import complete_capture
    capture = Capture(archive="Archive.today", original_url="https://www.youtube.com/watch?v=X",
                      archive_url="https://archive.ph/y5HcY", live="post", remark="the capture failed: gated")
    note = capture.note()
    assert note.startswith("> **The live post, not an archived copy**: the capture by Archive.today")
    assert "> Note: the capture failed: gated" in note
    response = ScrapingResponse(url="https://archive.ph/y5HcY",
                                content=ScrapedContent(markdown="m", capture=capture), output_format="markdown")
    assert not complete_capture("https://archive.ph/y5HcY", response)
