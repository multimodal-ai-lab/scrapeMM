"""The server side of search: the Serper provider, with its HTTP calls faked.

A live test against the real Serper runs only when SERPER_API_KEY is set, since every
call costs a credit.
"""

import asyncio
import json
import os
from pathlib import Path

import aiohttp
import pytest

pytest.importorskip("playwright", reason="needs the server dependencies")

from scrapemm.common import QuotaExceededError, RateLimitError  # noqa: E402
from scrapemm.search import SerperQuery, SerperResponse  # noqa: E402
from scrapemm.server import secrets as secrets_module  # noqa: E402
from scrapemm.server.search import (SEARCH_PROVIDERS, ProviderNotConfigured,  # noqa: E402
                                    get_provider)
from scrapemm.server.search import serper as serper_module  # noqa: E402

pytestmark = pytest.mark.server

DATA = Path(__file__).parent / "data"


def sample(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, status: int = 200, body=None, reason: str = "OK"):
        self.status = status
        self.reason = reason
        self._text = body if isinstance(body, str) else json.dumps(body)

    async def text(self) -> str:
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Answers each `post()` with the next outcome: a FakeResponse, or an exception
    to raise. Records every call."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def serper(monkeypatch):
    """The Serper provider, configured, and retrying without waiting."""
    monkeypatch.setattr(secrets_module, "_cache", {"serper_api_key": "sk-test"})
    monkeypatch.setattr(serper_module, "RETRY_DELAY", 0)
    return get_provider("serper")


async def test_a_web_search_asks_the_search_endpoint(serper):
    session = FakeSession(FakeResponse(200, sample("serper_search.json")))
    response = await serper.search(
        SerperQuery(q="apple inc", gl="us", num=10, tbs="qdr:w"), session)

    assert isinstance(response, SerperResponse)
    assert response.organic[0].link == "https://www.apple.com/"
    (call,) = session.calls
    assert call["url"] == "https://google.serper.dev/search"
    assert call["json"] == {"q": "apple inc", "gl": "us", "num": 10, "tbs": "qdr:w"}
    assert call["headers"]["X-API-KEY"] == "sk-test"


async def test_an_image_search_asks_the_images_endpoint(serper):
    session = FakeSession(FakeResponse(200, sample("serper_images.json")))
    response = await serper.search(SerperQuery(q="eiffel tower", type="images"), session)

    assert response.images[0].image_width == 1200
    assert session.calls[0]["url"] == "https://google.serper.dev/images"
    assert session.calls[0]["json"] == {"q": "eiffel tower"}


async def test_the_key_is_read_afresh_for_every_search(serper, monkeypatch):
    """Replacing the key in the web UI must take effect without a restart."""
    session = FakeSession(FakeResponse(200, {}), FakeResponse(200, {}))
    await serper.search(SerperQuery(q="x"), session)
    monkeypatch.setattr(secrets_module, "_cache", {"serper_api_key": "sk-new"})
    await serper.search(SerperQuery(q="x"), session)
    assert [c["headers"]["X-API-KEY"] for c in session.calls] == ["sk-test", "sk-new"]


async def test_without_a_key_serper_is_not_even_asked(serper, monkeypatch):
    monkeypatch.setattr(secrets_module, "_cache", {})
    session = FakeSession()
    with pytest.raises(ProviderNotConfigured, match="serper_api_key"):
        await serper.search(SerperQuery(q="x"), session)
    assert session.calls == []


async def test_a_provider_disabled_on_the_dashboard_is_not_asked(serper, monkeypatch):
    from scrapemm.server.search import ProviderDisabled

    monkeypatch.setattr("scrapemm.server.search.base.is_enabled", lambda name: False)
    session = FakeSession()
    with pytest.raises(ProviderDisabled, match="dashboard"):
        await serper.search(SerperQuery(q="x"), session)
    assert session.calls == []
    assert serper.describe()["enabled"] is False


async def test_an_invalid_query_is_not_sent(serper):
    session = FakeSession()
    with pytest.raises(ValueError):
        await serper.search(SerperQuery(q=" "), session)
    assert session.calls == []


@pytest.mark.parametrize("status, body, expected, message", [
    (401, {"message": "Unauthorized.", "statusCode": 401}, RuntimeError, "Secrets"),
    (403, {"message": "Unauthorized.", "statusCode": 403}, RuntimeError, "Secrets"),
    (400, {"message": "Not enough credits", "statusCode": 400}, QuotaExceededError,
     "no credits left"),
    (402, "Payment required", QuotaExceededError, "no credits left"),
    (400, {"message": "Query not allowed", "statusCode": 400}, ValueError,
     "Query not allowed"),
    (418, "", RuntimeError, "418"),
])
async def test_refusals_say_what_went_wrong(serper, status, body, expected, message):
    session = FakeSession(FakeResponse(status, body, reason="Nope"))
    with pytest.raises(expected, match=message):
        await serper.search(SerperQuery(q="x"), session)
    assert len(session.calls) == 1  # None of these improves by asking again


async def test_a_rate_limit_is_retried_before_giving_up(serper):
    session = FakeSession(*[FakeResponse(429, {"message": "Too many requests"})] * 3)
    with pytest.raises(RateLimitError, match="rate limit"):
        await serper.search(SerperQuery(q="x"), session)
    assert len(session.calls) == 1 + serper_module.MAX_RETRIES


async def test_a_passing_failure_of_serper_is_retried(serper):
    session = FakeSession(FakeResponse(503, "Service Unavailable"),
                          FakeResponse(200, sample("serper_search.json")))
    response = await serper.search(SerperQuery(q="x"), session)
    assert response.organic
    assert len(session.calls) == 2


async def test_a_lasting_failure_of_serper_is_reported(serper):
    session = FakeSession(*[FakeResponse(500, "", reason="Internal Server Error")] * 3)
    with pytest.raises(RuntimeError, match="500 Internal Server Error"):
        await serper.search(SerperQuery(q="x"), session)


async def test_a_dropped_connection_is_retried(serper):
    session = FakeSession(aiohttp.ClientConnectionError("reset"),
                          FakeResponse(200, sample("serper_search.json")))
    assert (await serper.search(SerperQuery(q="x"), session)).organic


async def test_an_unreachable_serper_is_reported(serper):
    session = FakeSession(*[aiohttp.ClientConnectionError("refused")] * 3)
    with pytest.raises(RuntimeError, match="Could not reach Serper"):
        await serper.search(SerperQuery(q="x"), session)


async def test_a_timeout_is_not_retried(serper):
    session = FakeSession(asyncio.TimeoutError())
    with pytest.raises(TimeoutError, match="did not answer"):
        await serper.search(SerperQuery(q="x"), session)
    assert len(session.calls) == 1


async def test_an_answer_that_is_not_json_is_serpers_fault(serper):
    """Not a ValueError, which would tell the caller their query was wrong."""
    session = FakeSession(FakeResponse(200, "<html>maintenance</html>"))
    with pytest.raises(RuntimeError, match="other than a JSON object"):
        await serper.search(SerperQuery(q="x"), session)


def test_providers_are_addressable():
    """The client addresses a provider by its query class's `provider`, the server by
    the provider's `name`: the two must agree, or queries go nowhere."""
    from scrapemm.server.secrets import SECRETS

    names = [provider.name for provider in SEARCH_PROVIDERS]
    assert len(names) == len(set(names))
    for provider in SEARCH_PROVIDERS:
        assert provider.name == provider.name.lower() and "/" not in provider.name
        assert provider.query_class.provider == provider.name
        assert provider.query_class.response_class.provider == provider.name
        assert set(provider.secret_names) <= set(SECRETS), (
            f"{provider.name} needs secrets the store does not offer")


def test_a_provider_describes_what_it_is_missing(monkeypatch):
    monkeypatch.setattr(secrets_module, "_cache", {})
    description = get_provider("serper").describe()
    assert description["configured"] is False
    assert description["missing_secrets"] == ["serper_api_key"]

    monkeypatch.setattr(secrets_module, "_cache", {"serper_api_key": "sk"})
    assert get_provider("serper").describe()["configured"] is True


@pytest.mark.skipif(not os.getenv("SERPER_API_KEY"),
                    reason="set SERPER_API_KEY to test against the real Serper")
@pytest.mark.parametrize("kind", ["search", "images"])
async def test_live_serper(kind, monkeypatch):
    monkeypatch.setattr(secrets_module, "_cache",
                        {"serper_api_key": os.environ["SERPER_API_KEY"]})
    async with aiohttp.ClientSession() as session:
        response = await get_provider("serper").search(
            SerperQuery(q="eiffel tower", type=kind, num=10), session)
    assert response.urls
    assert all(url.startswith("http") for url in response.urls)
    if kind == "images":
        assert response.image_urls


def _result_day(text: str):
    """The day of a result as Google shows it ("Apr 30, 2019"), or None for anything
    else. A relative one ("3 days ago") comes back as today: it is recent by definition."""
    import datetime as dt
    import re

    if re.search(r"\b(ago|yesterday|today)\b", text, re.IGNORECASE):
        return dt.date.today()
    for pattern in ("%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text.strip(), pattern).date()
        except ValueError:
            pass
    return None


@pytest.mark.skipif(not os.getenv("SERPER_API_KEY"),
                    reason="set SERPER_API_KEY to test against the real Serper")
async def test_live_serper_leaves_out_the_day_and_after(monkeypatch):
    """Google has to honour the date range, which only the real thing can show: every
    dated result must be from before the day given. A news-heavy query is used because
    its results carry dates."""
    import datetime as dt

    monkeypatch.setattr(secrets_module, "_cache",
                        {"serper_api_key": os.environ["SERPER_API_KEY"]})
    before = dt.date(2020, 1, 1)
    async with aiohttp.ClientSession() as session:
        response = await get_provider("serper").search(
            SerperQuery(q="climate protest news", num=20, before=before), session)

    days = {r.link: _result_day(r.date) for r in response.organic if r.date}
    dated = {link: day for link, day in days.items() if day is not None}
    assert dated, f"no result carried a readable date to check: {days}"
    late = {link: day for link, day in dated.items() if day >= before}
    assert not late, f"results from {before} or later came back: {late}"


# --- Scheduling: the gate and the shared session -----------------------------------

@pytest.fixture
def gate_limit(monkeypatch):
    """Sets max_search_concurrency, starting from a fresh gate."""
    from scrapemm.server.search import base

    monkeypatch.setattr(base, "_gate", None)
    monkeypatch.setattr(base, "_gate_limit", None)
    setting = {}
    monkeypatch.setattr(base, "get_config_var", lambda name, default=None:
                        setting.get(name, default))

    def set_limit(n):
        setting["max_search_concurrency"] = n
    return set_limit


@pytest.fixture
def slow_serper(serper, monkeypatch):
    """Serper answering only once `release` is set, counting searches in flight."""
    from scrapemm.server.search.serper import Serper

    state = {"now": 0, "most": 0, "sessions": [], "release": asyncio.Event()}

    async def fake(self, path, body, session):
        state["sessions"].append(session)
        state["now"] += 1
        state["most"] = max(state["most"], state["now"])
        try:
            await state["release"].wait()
            return sample("serper_search.json")
        finally:
            state["now"] -= 1

    monkeypatch.setattr(Serper, "_call", fake)
    return state


async def test_searches_beyond_the_limit_wait_their_turn(serper, slow_serper, gate_limit):
    gate_limit(2)
    searches = [asyncio.create_task(serper.search(SerperQuery(q=f"q{i}")))
                for i in range(6)]
    await asyncio.sleep(0.05)
    assert slow_serper["now"] == 2  # The other four are queued, not failed
    slow_serper["release"].set()
    responses = await asyncio.gather(*searches)
    assert all(r.organic for r in responses)
    assert slow_serper["most"] == 2


async def test_a_refused_search_does_not_wait_for_a_slot(serper, slow_serper, gate_limit,
                                                         monkeypatch):
    gate_limit(1)
    busy = asyncio.create_task(serper.search(SerperQuery(q="holds the only slot")))
    await asyncio.sleep(0.05)
    try:
        with pytest.raises(ValueError):
            await asyncio.wait_for(serper.search(SerperQuery(q="")), timeout=1)
        monkeypatch.setattr(secrets_module, "_cache", {})
        with pytest.raises(ProviderNotConfigured):
            await asyncio.wait_for(serper.search(SerperQuery(q="x")), timeout=1)
    finally:
        slow_serper["release"].set()
        await busy


async def test_a_changed_limit_takes_effect(gate_limit):
    from scrapemm.server.search.base import DEFAULT_MAX_SEARCH_CONCURRENCY, search_gate

    assert search_gate()._value == DEFAULT_MAX_SEARCH_CONCURRENCY
    gate_limit(3)
    assert search_gate()._value == 3
    gate_limit(0)  # Nonsense: at least one search must be able to run
    assert search_gate()._value == 1


async def test_searches_share_the_providers_session(serper, slow_serper, gate_limit):
    slow_serper["release"].set()
    await serper.search(SerperQuery(q="a"))
    await serper.search(SerperQuery(q="b"))
    first, second = slow_serper["sessions"]
    assert first is second is serper.session()
    assert not first.closed

    from scrapemm.server.search import close_sessions
    await close_sessions()
    assert first.closed
    assert serper.session() is not first  # A fresh one after closing
    await close_sessions()
