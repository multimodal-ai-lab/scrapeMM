"""The API surface, exercised in-process against the real FastAPI app.

The scraping engine is stubbed out: what is under test here is the transport and the
admin endpoints, and reaching out to the actual web would only make these flaky.
"""

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="needs the server dependencies")
pytest.importorskip("httpx", reason="needs httpx for the ASGI transport")

import httpx  # noqa: E402

from scrapemm.common import ScrapedContent, ScrapingResponse  # noqa: E402
from scrapemm.common.exceptions import (CaptchaEncounteredError,  # noqa: E402
                                        QuotaExceededError, RateLimitError,
                                        RetrievalFailed)

pytestmark = pytest.mark.server

AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture(scope="module")
def app(tmp_path_factory, monkeymodule):
    """The app, with its state in a throwaway directory and no real scraping."""
    config_dir = tmp_path_factory.mktemp("config")
    monkeymodule.setenv("SCRAPEMM_CONFIG_DIR", str(config_dir))
    monkeymodule.setenv("SCRAPEMM_API_KEY", "test-key")

    from scrapemm.server import paths
    monkeymodule.setattr(paths, "CONFIG_DIR", config_dir)
    monkeymodule.setattr(paths, "SECRETS_PATH", config_dir / "secrets")
    monkeymodule.setattr(paths, "MASTER_KEY_PATH", config_dir / "master.key")

    from scrapemm.server import secrets as secrets_module
    monkeymodule.setattr(secrets_module, "SECRETS_PATH", config_dir / "secrets")
    monkeymodule.setattr(secrets_module, "MASTER_KEY_PATH", config_dir / "master.key")
    monkeymodule.setattr(secrets_module, "_cache", None)

    from scrapemm.server.app import create_app
    return create_app()


@pytest.fixture(scope="module")
def monkeymodule():
    from _pytest.monkeypatch import MonkeyPatch
    patch = MonkeyPatch()
    yield patch
    patch.undo()


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_healthz_needs_no_key(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_api_requires_the_key(client):
    assert (await client.get("/v1/version")).status_code == 401
    assert (await client.get("/v1/version", headers=AUTH)).status_code == 200


async def test_wrong_key_is_refused(client):
    response = await client.get("/v1/version",
                                headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


async def test_secrets_never_return_their_values(client):
    await client.put("/v1/secrets/x_bearer_token", headers=AUTH,
                     json={"value": "super-secret"})

    response = await client.get("/v1/secrets", headers=AUTH)
    body = response.text
    assert "super-secret" not in body, "a secret value must never leave the server"

    entry = next(s for s in response.json()["secrets"] if s["name"] == "x_bearer_token")
    assert entry["is_set"] is True
    assert "value" not in entry

    await client.delete("/v1/secrets/x_bearer_token", headers=AUTH)


async def test_unknown_secret_is_refused(client):
    response = await client.put("/v1/secrets/not_a_secret", headers=AUTH,
                                json={"value": "x"})
    assert response.status_code == 404


async def test_server_managed_secret_cannot_be_set(client):
    """The Archive.today cookie is written by the server when a CAPTCHA is solved;
    letting somebody paste one by hand would only produce confusing failures."""
    response = await client.put("/v1/secrets/archive_today_cookie", headers=AUTH,
                                json={"value": "x"})
    assert response.status_code == 400


async def test_empty_secret_is_refused(client):
    response = await client.put("/v1/secrets/x_bearer_token", headers=AUTH,
                                json={"value": "   "})
    assert response.status_code == 400


async def test_config_rejects_unknown_settings(client):
    response = await client.patch("/v1/config", headers=AUTH, json={"nonsense": 1})
    assert response.status_code == 400
    assert "nonsense" in response.json()["detail"]


async def test_config_coerces_types(client):
    response = await client.patch("/v1/config", headers=AUTH,
                                  json={"hedging_delay": "5"})
    assert response.status_code == 200
    assert response.json()["config"]["hedging_delay"] == 5.0


async def test_config_rejects_uncoercible_values(client):
    response = await client.patch("/v1/config", headers=AUTH,
                                  json={"max_concurrency": "plenty"})
    assert response.status_code == 400


async def test_blacklist_round_trip(client):
    await client.post("/v1/blacklist/example.com", headers=AUTH,
                      json={"reason": "testing"})
    assert (await client.get("/v1/blacklist", headers=AUTH)
            ).json()["domains"]["example.com"] == "testing"

    removed = await client.delete("/v1/blacklist/example.com", headers=AUTH)
    assert removed.json()["removed"] is True
    assert "example.com" not in (
        await client.get("/v1/blacklist", headers=AUTH)).json()["domains"]


async def test_missing_media_is_404(client):
    assert (await client.get("/v1/media/image/999999", headers=AUTH)).status_code == 404


async def test_unknown_job_is_404(client):
    assert (await client.get("/v1/jobs/nope", headers=AUTH)).status_code == 404


# --- Retrieval streaming ----------------------------------------------------------

@pytest.fixture
def stub_engine(monkeypatch):
    """Replaces the engine with something predictable and instant."""
    async def fake(url, session, **kwargs):
        output_format = kwargs.get("output_format", "multimodal")
        if "fail" in url:
            return ScrapingResponse(
                url=url, content=None, output_format=output_format,
                errors={"firecrawl": RetrievalFailed("nothing there"),
                        "decodo": CaptchaEncounteredError("a Cloudflare challenge")})
        return ScrapingResponse(
            url=url, content=ScrapedContent(markdown="# Hi", html="<p>Hi</p>"),
            method="stub", output_format=output_format, retrieval_time=0.1)

    from scrapemm.server.api import retrieve as retrieve_api
    monkeypatch.setattr(retrieve_api, "retrieve_one", fake)


async def _lines(client, payload):
    async with client.stream("POST", "/v1/retrieve", headers=AUTH,
                             json=payload) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        return [json.loads(line) async for line in response.aiter_lines() if line.strip()]


async def test_retrieve_streams_header_results_and_summary(client, stub_engine):
    messages = await _lines(client, {
        "urls": ["https://example.com/a", "https://example.com/b"],
        "output_format": "markdown"})

    assert messages[0]["type"] == "header"
    assert messages[0]["protocol"] == 1
    assert messages[0]["total"] == 2
    assert messages[-1]["type"] == "summary"
    assert messages[-1]["succeeded"] == 2
    assert [m["type"] for m in messages[1:-1]] == ["result", "result"]


async def test_retrieve_reports_errors_per_method(client, stub_engine):
    messages = await _lines(client, {"urls": ["https://example.com/fail"],
                                     "output_format": "markdown"})
    errors = messages[1]["payload"]["errors"]
    assert errors["firecrawl"]["type"] == "RetrievalFailed"
    assert errors["decodo"]["type"] == "CaptchaEncounteredError"
    assert messages[-1]["failed"] == 1


async def test_retrieve_deduplicates_urls(client, stub_engine):
    """The same URL twice in one batch is scraped once; the client maps results back
    onto its own list by URL."""
    messages = await _lines(client, {
        "urls": ["https://example.com/x", "https://example.com/x"],
        "output_format": "markdown"})
    assert messages[0]["total"] == 1
    assert len([m for m in messages if m["type"] == "result"]) == 1


async def test_retrieve_rejects_unknown_output_format(client, stub_engine):
    """Refused before the stream opens, so the caller gets a plain error status rather
    than a 200 whose body turns out to be an apology."""
    response = await client.post("/v1/retrieve", headers=AUTH,
                                 json={"urls": ["https://example.com"],
                                       "output_format": "parquet"})
    assert response.status_code == 422


async def test_retrieve_rejects_an_empty_batch(client, stub_engine):
    response = await client.post("/v1/retrieve", headers=AUTH, json={"urls": []})
    assert response.status_code == 422


async def test_retrieve_records_a_job(client, stub_engine):
    messages = await _lines(client, {"urls": ["https://example.com/recorded"],
                                     "output_format": "markdown"})
    job_id = messages[0]["job_id"]

    job = (await client.get(f"/v1/jobs/{job_id}", headers=AUTH)).json()
    assert job["status"] == "completed"
    assert job["succeeded"] == 1
    result = job["results"][0]
    assert result["url"] == "https://example.com/recorded"
    # The job carries only what a result's header shows; the content comes on its own
    assert "content" not in result and result["stats"]["characters"] == len("# Hi")
    content = (await client.get(f"/v1/jobs/{job_id}/content", headers=AUTH,
                                params={"url": result["url"]})).json()["content"]
    assert content["markdown"] == "# Hi"
    missing = await client.get(f"/v1/jobs/{job_id}/content", headers=AUTH,
                               params={"url": "https://example.com/other"})
    assert missing.status_code == 404


# --- Search -----------------------------------------------------------------------

SERPER_SAMPLE = json.loads(
    (Path(__file__).parent / "data" / "serper_search.json").read_text(encoding="utf-8"))


@pytest.fixture
async def serper_key(client):
    """Serper configured the way a user would: through the Secrets API."""
    await client.put("/v1/secrets/serper_api_key", headers=AUTH, json={"value": "sk-test"})
    yield
    await client.delete("/v1/secrets/serper_api_key", headers=AUTH)


class SerperStub:
    """Stands in for Serper's API: records each call and answers with `answer`, or
    raises it if it is an exception."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.answer: dict | Exception = SERPER_SAMPLE

    async def call(self, path: str, body: dict) -> dict:
        self.calls.append((path, body))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def stub_serper(monkeypatch):
    from scrapemm.server.search.serper import Serper

    stub = SerperStub()

    async def fake(self, path, body, session):
        return await stub.call(path, body)

    monkeypatch.setattr(Serper, "_call", fake)
    return stub


async def test_search_lists_its_providers(client):
    response = await client.get("/v1/search", headers=AUTH)
    assert response.status_code == 200
    serper = next(p for p in response.json()["providers"] if p["name"] == "serper")
    assert serper["configured"] is False
    assert serper["missing_secrets"] == ["serper_api_key"]


async def test_search_requires_the_key(client):
    response = await client.post("/v1/search/serper", json={"q": "x"})
    assert response.status_code == 401


async def test_search_with_an_unknown_provider_is_404(client):
    response = await client.post("/v1/search/nope", headers=AUTH, json={"q": "x"})
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)
    assert "serper" in response.json()["detail"]
    assert response.json()["error"]["type"] == "ValueError"


async def test_search_without_the_providers_key_says_so(client, stub_serper):
    response = await client.post("/v1/search/serper", headers=AUTH, json={"q": "x"})
    assert response.status_code == 503
    assert "serper_api_key" in response.json()["detail"]
    assert stub_serper.calls == []


@pytest.mark.parametrize("body", [{"q": "x", "type": "videos"}, {"num": 5},
                                  {"q": "x", "num": "many"}])
async def test_search_rejects_a_malformed_query(client, body):
    response = await client.post("/v1/search/serper", headers=AUTH, json=body)
    assert response.status_code == 422


async def test_search_rejects_an_invalid_query(client, serper_key, stub_serper):
    response = await client.post("/v1/search/serper", headers=AUTH, json={"q": "x", "num": 0})
    assert response.status_code == 400
    assert "between 1 and 100" in response.json()["detail"]
    assert stub_serper.calls == []


async def test_search_answers_in_the_providers_own_format(client, serper_key, stub_serper):
    response = await client.post("/v1/search/serper", headers=AUTH,
                                 json={"q": "apple inc", "type": "search", "gl": "us"})
    assert response.status_code == 200
    assert response.json() == SERPER_SAMPLE
    assert stub_serper.calls == [("/search", {"q": "apple inc", "gl": "us"})]


@pytest.mark.parametrize("raised, status", [
    (RateLimitError("slow down"), 429),
    (QuotaExceededError("no credits"), 402),
    (TimeoutError("too slow"), 504),
    (ValueError("bad query"), 400),
    (RuntimeError("Serper rejected the API key"), 502),
    (KeyError("a bug"), 500),
])
async def test_search_failures_keep_their_meaning(client, serper_key, stub_serper,
                                                  raised, status):
    stub_serper.answer = raised
    response = await client.post("/v1/search/serper", headers=AUTH, json={"q": "x"})
    assert response.status_code == status
    assert isinstance(response.json()["detail"], str)
    assert response.json()["error"]["type"] == type(raised).__name__


async def test_a_rejected_provider_key_does_not_look_like_a_rejected_api_key(
        client, serper_key, stub_serper):
    """The UI signs out on a 401, which would be the wrong key to blame."""
    stub_serper.answer = RuntimeError("Serper rejected the API key (403).")
    response = await client.post("/v1/search/serper", headers=AUTH, json={"q": "x"})
    assert response.status_code != 401


async def test_a_search_is_no_job(client, serper_key, stub_serper):
    before = (await client.get("/v1/jobs", headers=AUTH)).json()["total"]
    await client.post("/v1/search/serper", headers=AUTH, json={"q": "x"})
    assert (await client.get("/v1/jobs", headers=AUTH)).json()["total"] == before


async def test_a_search_key_refreshes_only_its_own_card(client, monkeypatch):
    """Changing a secret re-probes the cards it affects, and not every integration."""
    from scrapemm.server import status

    monkeypatch.setitem(status._cache, "youtube", "cached status")
    monkeypatch.setitem(status._cache, "serper", "cached status")
    await client.put("/v1/secrets/serper_api_key", headers=AUTH, json={"value": "sk"})
    assert "serper" not in status._cache
    await client.delete("/v1/secrets/serper_api_key", headers=AUTH)
    assert status._cache.get("youtube") == "cached status"


async def test_a_secret_no_card_uses_leaves_the_dashboard_alone(client, monkeypatch):
    """`invalidate()` with no names means "everything"; an empty list of affected cards
    must not end up there."""
    from scrapemm.server import status
    from scrapemm.server.api import admin

    monkeypatch.setattr(admin.status_module, "secrets_to_integrations", lambda name: [])
    monkeypatch.setitem(status._cache, "youtube", "cached status")
    await client.put("/v1/secrets/serper_api_key", headers=AUTH, json={"value": "sk"})
    await client.delete("/v1/secrets/serper_api_key", headers=AUTH)
    assert status._cache.get("youtube") == "cached status"


async def test_a_search_provider_disabled_on_the_dashboard_refuses(client, serper_key,
                                                                    stub_serper):
    toggled = await client.put("/v1/integrations/serper/enabled", headers=AUTH,
                               json={"enabled": False})
    assert toggled.status_code == 200
    assert toggled.json()["state"] == "disabled"
    try:
        listed = (await client.get("/v1/search", headers=AUTH)).json()["providers"]
        assert next(p for p in listed if p["name"] == "serper")["enabled"] is False
        response = await client.post("/v1/search/serper", headers=AUTH, json={"q": "x"})
        assert response.status_code == 503
        assert "disabled" in response.json()["detail"]
        assert stub_serper.calls == []
    finally:
        await client.put("/v1/integrations/serper/enabled", headers=AUTH,
                         json={"enabled": True})


async def test_search_passes_before_on_as_a_date_range(client, serper_key, stub_serper):
    response = await client.post("/v1/search/serper", headers=AUTH,
                                 json={"q": "x", "before": "2024-05-01"})
    assert response.status_code == 200
    assert stub_serper.calls == [
        ("/search", {"q": "x", "tbs": "cdr:1,cd_min:1/1/1900,cd_max:4/30/2024"})]


@pytest.mark.parametrize("body, status", [
    ({"q": "x", "before": "2024-13-01"}, 422),
    ({"q": "x", "before": "2024-05-01", "tbs": "qdr:w"}, 400),
])
async def test_search_refuses_a_before_it_cannot_use(client, serper_key, stub_serper,
                                                    body, status):
    response = await client.post("/v1/search/serper", headers=AUTH, json=body)
    assert response.status_code == status
    assert stub_serper.calls == []


async def test_search_leaves_out_excluded_sites(client, serper_key, stub_serper):
    response = await client.post("/v1/search/serper", headers=AUTH, json={
        "q": "apple inc", "exclude_sites": ["wikipedia.org", "https://finance.yahoo.com/"]})
    assert response.status_code == 200
    assert stub_serper.calls == [
        ("/search", {"q": "apple inc -site:wikipedia.org -site:finance.yahoo.com"})]
    links = [r["link"] for r in response.json()["organic"]]
    assert links == ["https://www.apple.com/"]
    assert "answerBox" not in response.json()


async def test_search_refuses_an_excluded_site_that_is_no_domain(client, serper_key,
                                                                stub_serper):
    response = await client.post("/v1/search/serper", headers=AUTH,
                                 json={"q": "x", "exclude_sites": ["not a domain"]})
    assert response.status_code == 400
    assert stub_serper.calls == []
