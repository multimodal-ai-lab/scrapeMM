"""The Internet Archive's video captures are read from the raw capture first: the video
its markup names, without replaying the capture in the browser -- which took 90-170 s
for the test suite's TikTok and Kwai captures, against a few seconds this way."""

import pytest

from scrapemm.server.integrations import archive_org
from scrapemm.server.integrations.archive_org import ArchiveOrg, _raw_capture_tried

pytestmark = pytest.mark.server

CAPTURE = "https://web.archive.org/web/20231020082452/https://www.tiktok.com/@a/video/7291677940834766113"
MARKUP = '<html><title>A video</title><body><p>Caption</p>' \
         '<script>{"playAddr":"https:\\/\\/v16.tiktok.com\\/video\\/abc.mp4"}</script></body></html>'


@pytest.fixture
def replay(monkeypatch):
    """Records whether the browser replay was asked, and whether it would look at the
    capture's markup again."""
    calls = []

    async def browser_get(self, url, **kwargs):
        calls.append(_raw_capture_tried.get())
        return "replayed"
    monkeypatch.setattr(archive_org.HeadedBrowser, "_get", browser_get)
    return calls


@pytest.mark.asyncio
async def test_a_video_named_in_the_raw_capture_needs_no_replay(monkeypatch, replay):
    async def raw(timestamp, original):
        return timestamp, MARKUP
    fetched = []

    async def download(url, session, max_video_size):
        fetched.append(url)
        return "VIDEO"
    monkeypatch.setattr(archive_org, "_raw_capture", raw)
    monkeypatch.setattr(archive_org, "_download_archived_video", download)
    monkeypatch.setattr(archive_org, "MultimodalSequence", list)  # A stand-in for the items
    content = await ArchiveOrg()._get(CAPTURE)
    assert replay == []  # Never replayed
    assert fetched == ["https://web.archive.org/web/20231020082452id_/https://v16.tiktok.com/video/abc.mp4"]
    assert content.multimodal[-1] == "VIDEO" and "Caption" in content.markdown


@pytest.mark.asyncio
async def test_without_a_video_the_capture_is_replayed_without_reading_the_markup_again(
        monkeypatch, replay):
    async def raw(timestamp, original):
        return timestamp, "<html><body>No video named here</body></html>"
    monkeypatch.setattr(archive_org, "_raw_capture", raw)
    assert await ArchiveOrg()._get(CAPTURE) == "replayed"
    assert replay == [True]  # The replay knows the markup was looked at
    assert _raw_capture_tried.get() is False  # ...and only for this retrieval


@pytest.mark.asyncio
async def test_pages_without_video_are_replayed_straight_away(monkeypatch, replay):
    async def raw(timestamp, original):
        raise AssertionError("A page without a video is not read raw")
    monkeypatch.setattr(archive_org, "_raw_capture", raw)
    page = "https://web.archive.org/web/20260602042101/https://www.threads.com/@a/post/DZCPCucmsFy"
    assert await ArchiveOrg()._get(page) == "replayed"
    assert replay == [False]
