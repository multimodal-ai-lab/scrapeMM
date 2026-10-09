"""Whether a retrieval falls back to archiving services: `enable_archives_fallback`, which by default
follows `prioritize` (on for completeness, off for speed)."""

import pytest

from scrapemm.server import engine
from scrapemm.common.exceptions import RetrievalFailed

pytestmark = pytest.mark.server

URL = "https://example.org/some-article"


@pytest.fixture
def archives_tried(monkeypatch):
    """Every live method fails; the archive stage records which archives it was asked for."""
    asked: list[list[str]] = []

    async def reachable(url):
        return "ok", ""

    async def failing(url, routine, method_name, session):
        return RetrievalFailed("down")

    async def record(archives, *args, **kwargs):
        asked.append(list(archives))
        return None

    monkeypatch.setattr(engine.reachability, "check", reachable)
    monkeypatch.setattr(engine, "_execute", failing)
    monkeypatch.setattr(engine, "_run_archives", record)
    return asked


@pytest.mark.parametrize("kwargs, expected", [
    (dict(), True),                                         # completeness: on by default
    (dict(prioritize="speed"), False),                      # speed: off by default
    (dict(prioritize="speed", enable_archives_fallback=True), True),
    (dict(prioritize="completeness", enable_archives_fallback=False), False),
])
async def test_fallback_to_archives(archives_tried, kwargs, expected):
    response = await engine.retrieve(URL, show_progress=False, use_cache=False, **kwargs)
    assert response.content is None
    tried = [name for asked in archives_tried for name in asked]
    assert bool(tried) is expected


async def test_archive_named_in_methods_is_tried_regardless(archives_tried):
    await engine.retrieve(URL, show_progress=False, use_cache=False, methods=["wayback"],
                          prioritize="speed", enable_archives_fallback=False)
    assert [name for asked in archives_tried for name in asked] == ["wayback"]
