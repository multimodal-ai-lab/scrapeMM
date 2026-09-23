"""Tests for the Archive.today snapshot-resolving utility
(`scrapemm.server.integrations.archive_today`), which maps any mirror snapshot URL to
the original URL, capture time and content hash without passing the access check.

The short ids used here are the same captures the integration test suite relies on;
snapshots never change, so their resolved values are stable. Only the resolving helpers
are exercised (via the ungated cse.js and capture-listing endpoints) — no session, no
browser, no CAPTCHA.
"""
import pytest

from scrapemm.server.integrations import archive_today
from scrapemm.server.integrations.archive_today import (
    Snapshot, canonicalize_url, identify_snapshot, resolve_snapshot,
)

pytestmark = pytest.mark.server


@pytest.fixture(autouse=True)
def isolated_snapshot_cache(tmp_path, monkeypatch):
    """Point the permanent snapshot cache at a temp file so tests neither read nor
    pollute the user's real cache in the config directory."""
    monkeypatch.setattr(archive_today, "_snapshots",
                        archive_today._SnapshotCache(tmp_path / "snapshots.json"))


# --- Offline: pure URL/dataclass logic -------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("https://archive.ph/uTVE4", "https://archive.ph/uTVE4"),
    ("https://archive.is/uTVE4", "https://archive.ph/uTVE4"),       # mirror -> canonical
    ("http://archive.today/uTVE4", "https://archive.ph/uTVE4"),      # http -> https, mirror
    ("https://archive.md/SI9Yy/again", "https://archive.ph/SI9Yy/again"),  # sub-path kept
    ("http://archive.today/2022.05.05-091515/https:/twitter.com/x/status/1",
     "https://archive.ph/2022.05.05-091515/https:/twitter.com/x/status/1"),  # long form kept
])
def test_canonicalize_url(url, expected):
    assert canonicalize_url(url) == expected


def test_snapshot_properties():
    snapshot = Snapshot(
        id="uTVE4",
        original_url="https://x.com/krishnakamal077/status/1917647830161805697",
        captured_at="2025-05-01T09:22:32+00:00",
        hash="da2c6541801809f1b665e8992f7d214621ec9443",
    )
    assert snapshot.canonical_url == "https://archive.ph/uTVE4"
    assert snapshot.screenshot_url == (
        "https://archive.ph/uTVE4/da2c6541801809f1b665e8992f7d214621ec9443/scr.png")


# --- Online: resolving via the ungated endpoints ---------------------------------------

@pytest.mark.parametrize("url, short_id, original_url", [
    ("https://archive.is/uTVE4", "uTVE4",
     "https://x.com/krishnakamal077/status/1917647830161805697"),
    ("https://archive.ph/0VrgI", "0VrgI",
     "https://www.facebook.com/southafricadaily247/posts/2056369861197540"),
    ("https://archive.md/movd4", "movd4", "https://www.alkhabour.com/ar/news/single/2158"),
    ("https://archive.vn/Edqcv", "Edqcv", "https://bloomnews24.com/6913/"),
    ("http://archive.today/6OttS", "6OttS",
     "https://www.facebook.com/groups/2263333770436795/permalink/2772779999492167"),
])
async def test_identify_snapshot_short_url(url, short_id, original_url):
    snapshot = await identify_snapshot(url)
    assert snapshot is not None
    assert snapshot.id == short_id
    assert snapshot.original_url == original_url
    assert len(snapshot.hash) == 40  # 40-hex content hash addresses the screenshot


async def test_identify_snapshot_long_form_url():
    """A long-form URL (timestamp + original URL, on any mirror, with the scheme's slash
    mangled to one) resolves to its short id via the capture listing."""
    url = ("http://archive.today/2022.05.05-091515/"
           "https:/twitter.com/SamvanRooy1/status/1521438261130014721")
    snapshot = await identify_snapshot(url)
    assert snapshot is not None
    assert snapshot.id == "rR9nq"
    assert snapshot.original_url == "https://twitter.com/SamvanRooy1/status/1521438261130014721"


async def test_identify_snapshot_is_mirror_agnostic():
    """The same id resolves identically no matter which mirror it is requested on."""
    from_is = await identify_snapshot("https://archive.is/uTVE4")
    from_md = await identify_snapshot("https://archive.md/uTVE4")
    assert from_is is not None and from_md is not None
    assert (from_is.original_url, from_is.hash) == (from_md.original_url, from_md.hash)


@pytest.mark.parametrize("url", [
    "https://archive.ph/zzzzz",  # well-formed id that does not exist
    "https://archive.ph/",       # not a snapshot URL at all
])
async def test_identify_snapshot_returns_none_for_non_snapshot(url):
    assert await identify_snapshot(url) is None


async def test_resolve_snapshot_is_cached_permanently():
    """A resolved snapshot is written to the permanent cache and served from it after."""
    assert archive_today._snapshots.get("uTVE4") is None  # isolated, starts empty
    first = await resolve_snapshot("uTVE4")
    assert first is not None
    cached = archive_today._snapshots.get("uTVE4")
    assert cached is not None and cached.original_url == first.original_url
