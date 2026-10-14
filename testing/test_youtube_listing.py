"""YouTube channels and playlists are listed (thumbnails, not streams); videos are not."""

import pytest

from scrapemm.server.integrations.ytdlp import _clock, _listing_url, _thumbnail_url, is_youtube_listing

pytestmark = pytest.mark.server


@pytest.mark.parametrize("url, listing", [
    ("https://www.youtube.com/@UnderstandingWarOrg/videos", True),
    ("https://www.youtube.com/@UnderstandingWarOrg", True),
    ("https://m.youtube.com/channel/UCwRudqD0bS_hrA0DfU4BwOw/shorts", True),
    ("https://www.youtube.com/playlist?list=PL123", True),
    ("https://www.youtube.com/watch?v=RxJtYsCQ0jo", False),
    ("https://www.youtube.com/shorts/cE0zgN6pYOc", False),
    ("https://youtu.be/RxJtYsCQ0jo", False),
    ("https://example.com/@someone", False),
])
def test_listings_are_told_from_videos(url, listing):
    assert is_youtube_listing(url) is listing


def test_a_channel_home_lists_its_videos():
    assert _listing_url("https://www.youtube.com/@ISW") == "https://www.youtube.com/@ISW/videos"
    assert _listing_url("https://www.youtube.com/@ISW/featured") == "https://www.youtube.com/@ISW/videos"
    assert _listing_url("https://www.youtube.com/@ISW/shorts") == "https://www.youtube.com/@ISW/shorts"


def test_the_thumbnail_and_duration():
    entry = {"id": "x", "thumbnails": [{"url": "s", "width": 360}, {"url": "m", "width": 720},
                                       {"url": "l", "width": 1280}]}
    assert _thumbnail_url(entry) == "m"
    assert _thumbnail_url({"id": "x"}) == "https://i.ytimg.com/vi/x/hqdefault.jpg"
    assert (_clock(330), _clock(3725), _clock(None)) == ("5:30", "1:02:05", "")
