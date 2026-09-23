"""The API surface, exercised in-process against the real FastAPI app.

The scraping engine is stubbed out: what is under test here is the transport and the
admin endpoints, and reaching out to the actual web would only make these flaky.
"""

import json

import pytest

pytest.importorskip("fastapi", reason="needs the server dependencies")
pytest.importorskip("httpx", reason="needs httpx for the ASGI transport")

import httpx  # noqa: E402

from scrapemm.common import ScrapedContent, ScrapingResponse  # noqa: E402
from scrapemm.common.exceptions import (CaptchaEncounteredError,  # noqa: E402
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
    assert job["results"][0]["url"] == "https://example.com/recorded"
    assert job["results"][0]["content"]["markdown"] == "# Hi"
