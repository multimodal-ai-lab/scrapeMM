"""A video stream that stalls on its way (a read timeout) is no missing video: another
route is tried, and the proxy that stalled keeps its standing."""

import pytest
from yt_dlp.utils import DownloadError

from scrapemm.common import AccessBlockedError, RetrievalFailed
from scrapemm.server.integrations.ytdlp import STREAM_STALLED, _classify
from scrapemm.server.proxy import ip_blocked, worth_another_route

pytestmark = pytest.mark.server

# Production, 2026-10-15, through a proxy, after 34 s
STALL = ("ERROR: \r[download] Got error: HTTPSConnectionPool(host='rr3---sn-4g5e6nss.googlevideo.com', "
         "port=443): Read timed out. (read timeout=15.0). Giving up after 1 retries")


def test_a_stalled_stream_is_tried_elsewhere_without_blame():
    error = _classify(DownloadError(STALL))
    assert isinstance(error, RetrievalFailed) and str(error).startswith(STREAM_STALLED)
    assert worth_another_route(error) and not ip_blocked(error)


def test_a_refused_stream_still_is_one():
    error = _classify(DownloadError("ERROR: [download] Got error: HTTP Error 403: Forbidden"))
    assert isinstance(error, AccessBlockedError) and ip_blocked(error)


def test_a_firewalls_not_acceptable_is_tried_through_a_proxy():
    assert ip_blocked(AccessBlockedError("https://www.eci.gov.in/x answered HTTP 406 with next to no content."))
