"""Sites behind a CAPTCHA for every visitor (`challenges.ALWAYS_GATED`) are never
scraped: their URLs go straight into the queue for a human -- unless an integration
serves the site (researchgate.net: OpenAlex), which does not go where the CAPTCHA is."""

import aiohttp
import pytest

from scrapemm import CaptchaEncounteredError
from scrapemm.server import challenges, engine
from scrapemm.server.cache import clear_cache
from scrapemm.server.challenges import ChallengeStore

pytestmark = pytest.mark.server

URL = "https://www.gated.test/article/123"


async def test_an_always_gated_site_is_queued_without_scraping(monkeypatch, tmp_path):
    store = ChallengeStore(path=tmp_path / "challenges.json")
    monkeypatch.setattr(challenges, "store", store)
    monkeypatch.setattr(challenges, "ALWAYS_GATED", {"gated.test": "DataDome"})
    monkeypatch.setattr(engine.blacklist, "reason", lambda domain: None)
    clear_cache()

    async def scraped(*args, **kwargs):
        raise AssertionError("A method was tried.")

    monkeypatch.setattr(engine.browser, "_get", scraped)
    monkeypatch.setattr(engine, "_plain_http", scraped)

    async with aiohttp.ClientSession() as session:
        response = await engine.retrieve_one(URL, session, methods=["browser"], use_cache=False)

    assert not response.success
    assert isinstance(response.errors["scrapemm"], CaptchaEncounteredError)
    challenge = store.get("gated.test")
    assert challenge.captcha == "DataDome"
    assert [p.url for p in challenge.pending] == [URL]


def test_a_site_an_integration_serves_is_not_held(tmp_path):
    store = ChallengeStore(path=tmp_path / "challenges.json")
    assert "researchgate.net" in challenges.ALWAYS_GATED
    assert not store.holds("researchgate.net")
