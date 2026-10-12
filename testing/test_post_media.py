"""Small media are left out of web pages as icons, logos and avatars -- but not out of a
platform post: what an integration takes from a post is the post's own (Telegram's small
photos and stickers were dropped). Both minimums are settings."""

import pytest

from scrapemm.server import config
from scrapemm.server.download import images
from scrapemm.server.download.images import is_too_small, min_media_side, post_media
from scrapemm.server.integrations.base import RetrievalIntegration

pytestmark = pytest.mark.server


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    values: dict = {}
    monkeypatch.setattr(config, "_config", values)
    return values


def test_page_media_keep_the_minimum_post_media_do_not(settings):
    assert min_media_side() == 128 and is_too_small(100, 400)
    with post_media():
        assert min_media_side() == 0 and not is_too_small(100, 400)
    assert is_too_small(100, 400)  # Only within


def test_both_minimums_are_settings(settings):
    settings.update(min_media_side=64, min_post_media_side=50)
    assert not is_too_small(100, 400)
    with post_media():
        assert is_too_small(40, 400) and not is_too_small(60, 400)


class _Platform(RetrievalIntegration):
    name, domains, post_media = "Platform", ["platform.com"], True

    async def _connect(self):
        self.connected = True

    async def _get(self, url, **kwargs):
        return min_media_side()


class _Page(_Platform):
    name, domains, post_media = "Page", ["page.com"], False


@pytest.mark.asyncio
async def test_platform_integrations_collect_post_media(settings):
    assert await _Platform().get("https://platform.com/post/1") == 0
    assert await _Page().get("https://page.com/") == 128


@pytest.mark.asyncio
async def test_a_post_image_the_cache_knows_as_too_small_is_fetched_after_all(monkeypatch):
    """The archive cache remembers a too-small image as "no item"; for a post, that verdict
    must not stand."""
    asked = []

    class Cache:
        async def item(self, url, variant="", ignore_small=True, max_bytes=None):
            asked.append(ignore_small)
            return (True, None) if ignore_small else (False, None)

        async def keep_item(self, *args, **kwargs):
            pass

    import scrapemm.server.cache as cache_module
    monkeypatch.setattr(cache_module, "immutable", Cache())

    async def nothing(*args, **kwargs):
        return None
    monkeypatch.setattr(images, "request_static", nothing)
    await images.download_image("https://x.example/a.jpg", None)
    with post_media():
        await images.download_image("https://x.example/a.jpg", None)
    assert asked == [True, False]
