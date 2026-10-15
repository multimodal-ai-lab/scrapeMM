"""Megalodon (megalodon.jp): captures read from their snapshot with a plain request, and
their addresses kept as they are."""

import pytest

from scrapemm.common.exceptions import TargetUnavailableError
from scrapemm.server.integrations import megalodon
from scrapemm.server.integrations.megalodon import Megalodon
from scrapemm.server.util import preprocess_url

pytestmark = pytest.mark.server

EMBED = ("https://megalodon.jp/2026-0223-2237-14/https://www.facebook.com:443/plugins/post.php?"
         "href=https%3A%2F%2Fwww.facebook.com%2Fpermalink.php%3Fstory_fbid%3Dpfbid02%26id%3D1&show_text=true&")


def test_megalodon_addresses_keep_their_encoding_others_are_decoded():
    assert preprocess_url(EMBED) == EMBED  # Decoded, the embed's href names another address
    assert preprocess_url("https://gyo.tc/a%2Fb") == "https://gyo.tc/a%2Fb"
    assert preprocess_url("https://example.com/a%20b") == "https://example.com/a b"


class _Response:
    def __init__(self, status=200, content_type="text/html", text="", url=""):
        self.status_code, self.text, self.url = status, text, url
        self.headers = {"content-type": content_type}
        self.content = text.encode()


@pytest.fixture
def fetched(monkeypatch):
    """Answers requests from a table, and records what was asked."""
    asked, answers = [], {}

    async def curl_get(url, **kwargs):
        asked.append(url)
        return answers[url]
    monkeypatch.setattr(megalodon, "curl_get", curl_get)
    return asked, answers


@pytest.mark.asyncio
async def test_a_capture_is_read_from_its_snapshot_and_a_missing_one_is_unavailable(fetched):
    asked, answers = fetched
    snapshot = EMBED.replace("megalodon.jp/", "megalodon.jp/ref/", 1)
    answers[snapshot] = _Response(text="<html><body>魚拓が見つかりません</body></html>")
    with pytest.raises(TargetUnavailableError):
        await Megalodon()._get(EMBED)
    assert asked == [snapshot]  # The snapshot, under the very address asked for


@pytest.mark.asyncio
async def test_a_short_link_leads_to_its_capture(fetched):
    asked, answers = fetched
    answers["https://gyo.tc/AbCd"] = _Response(
        text='<meta property="og:url" content="https://megalodon.jp/2026-0417-1631-38/https://x.com:443/a/status/1">')
    assert await Megalodon()._capture_url("https://gyo.tc/AbCd") == \
        "https://megalodon.jp/2026-0417-1631-38/https://x.com:443/a/status/1"
