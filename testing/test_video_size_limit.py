"""Videos over `max_video_size`: their size is known before the download where yt-dlp can
tell (HLS formats by their direct twin), and a video left out says so in the text."""

import pytest

from scrapemm.server.integrations.ytdlp import (VIDEO_OMITTED, _format_size, _selection_size,
                                                _size_guard, _TooLarge, compose_data_to_sequence)

pytestmark = pytest.mark.server

# As YouTube lists a 38-minute video (plzE2jioaNU): the HLS format states no size, its
# direct twin of the same resolution and codec does
HLS_720 = {"format_id": "232", "height": 720, "vcodec": "avc1.4D401F", "acodec": "none",
           "tbr": 2577.05}
DASH_720 = {"format_id": "136", "height": 720, "vcodec": "avc1.4D401F", "acodec": "none",
            "filesize": 280584192}
VP9_720 = {"format_id": "247", "height": 720, "vcodec": "vp9", "acodec": "none",
           "filesize": 178146612}
AUDIO = {"format_id": "140-7", "vcodec": "none", "acodec": "mp4a.40.2", "filesize": 37263666}
FORMATS = [HLS_720, DASH_720, VP9_720, AUDIO]


def test_an_hls_format_is_sized_by_its_twin():
    assert _format_size(HLS_720, FORMATS) == 280584192
    info = {"formats": FORMATS, "requested_formats": [HLS_720, AUDIO], "duration": 2302}
    assert _selection_size(info) == 280584192 + 37263666


def test_an_unknown_size_is_estimated_only_on_request():
    alone = [HLS_720, AUDIO]
    info = {"formats": alone, "requested_formats": alone, "duration": 2302}
    assert _selection_size(info) is None  # Left to the progress hook
    assert _selection_size(info, estimate=True) == int(2577.05 * 125 * 2302) + 37263666


def test_the_guard_stops_a_download_that_outgrows_the_limit():
    hook = _size_guard(100)
    hook({"status": "downloading", "filename": "v", "downloaded_bytes": 60})
    hook({"status": "downloading", "filename": "a", "downloaded_bytes": 30})
    with pytest.raises(_TooLarge):
        hook({"status": "downloading", "filename": "v", "downloaded_bytes": 80})


async def test_a_video_left_out_says_why():
    metadata = {"uploader": "x", VIDEO_OMITTED: "319 MB, more than the size limit of 100 MB"}
    sequence = await compose_data_to_sequence(metadata, None, None, "YouTube")
    assert "Video omitted: 319 MB, more than the size limit of 100 MB" in str(sequence)
    assert "omitted" not in str(await compose_data_to_sequence({"uploader": "x"}, None, None,
                                                               "YouTube"))
