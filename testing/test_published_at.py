"""When content was published, as the source says: from platform data, post IDs, or the
page's own metadata (see `ScrapedContent.published_at`)."""

import pytest

from scrapemm.common.wire import ContentPayload, ResponsePayload
from scrapemm.server.published import normalize, published_from_html

pytestmark = pytest.mark.server


def test_from_page_metadata():
    assert published_from_html('<meta property="article:published_time" content="2026-02-10T18:25:00+01:00" />') \
        == "2026-02-10T17:25:00+00:00"
    assert published_from_html('<meta name="citation_publication_date" content="2013">') == "2013"  # PMC
    assert published_from_html('<meta content="2009" name="DC.Date">') == "2009"  # Bookshelf
    assert published_from_html('<script type="application/ld+json">{"datePublished": "2025-07-24"}</script>') \
        == "2025-07-24"
    assert published_from_html("<p>No date here</p>") is None


def test_precision_is_kept():
    assert normalize("2020 Feb 3") == "2020-02-03"
    assert normalize("1867 Jan") == "1867-01"
    assert normalize("2013 Spring") == "2013"
    assert normalize("2026-01-01 12:00:00") == "2026-01-01T12:00:00+00:00"


def test_from_post_ids():
    from scrapemm.server.integrations.instagram import shortcode_time
    from scrapemm.server.integrations.x import tweet_time
    assert str(shortcode_time("DV5h_RaDEx7")) == "2026-03-15 08:47:41+00:00"  # yt-dlp says the same
    assert str(tweet_time(1819424956826165467)) == "2024-08-02 17:28:07+00:00"
    assert tweet_time(20) is None  # Before snowflake IDs


def test_from_yt_dlp():
    from scrapemm.server.integrations.ytdlp import posted_text, published_at
    assert published_at({"timestamp": 1773564461, "upload_date": "20260315"}) == "2026-03-15T08:47:41+00:00"
    assert published_at({"upload_date": "20260315"}) == "2026-03-15"
    assert posted_text("2026-03-15T08:47:41+00:00") == "2026-03-15 08:47 UTC"


def test_travels_to_the_client():
    payload = ResponsePayload(url="u", content=ContentPayload(markdown="m", published_at="2013"))
    assert ResponsePayload.from_dict(payload.to_dict()).to_response().published_at == "2013"
