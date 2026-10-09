"""API keys and their roles: Root, Admin, Client. Exercised in-process against the app."""

from unittest.mock import ANY

import pytest

pytest.importorskip("fastapi", reason="needs the server dependencies")
pytest.importorskip("httpx", reason="needs httpx for the ASGI transport")

import httpx  # noqa: E402

pytestmark = pytest.mark.server

ROOT_KEY = "root-key"


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


ROOT = bearer(ROOT_KEY)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPEMM_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SCRAPEMM_API_KEY", ROOT_KEY)

    from scrapemm.server import auth, paths
    monkeypatch.setattr(paths, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth, "_api_key", None)
    monkeypatch.setattr(auth, "_store", None)

    from scrapemm.server import secrets as secrets_module
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", tmp_path / "secrets")
    monkeypatch.setattr(secrets_module, "MASTER_KEY_PATH", tmp_path / "master.key")
    monkeypatch.setattr(secrets_module, "_cache", None)

    from scrapemm.server.app import create_app
    return create_app()


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def create(client, name: str, role: str, as_: dict = ROOT) -> str:
    response = await client.post("/v1/api-keys", headers=as_, json={"name": name, "role": role})
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def test_me(client):
    assert (await client.get("/v1/me", headers=ROOT)).json()["role"] == "root"
    token = await create(client, "Pipeline", "client")
    assert (await client.get("/v1/me", headers=bearer(token))).json() == {
        "id": ANY, "name": "Pipeline", "role": "client"}


async def test_unknown_key_is_rejected(client):
    assert (await client.get("/v1/me", headers=bearer("nope"))).status_code == 401


async def test_keys_are_stored_hashed(client, tmp_path):
    token = await create(client, "Pipeline", "client")
    stored = (tmp_path / "api_keys.json").read_text(encoding="utf-8")
    assert token not in stored and "Pipeline" in stored
    listed = (await client.get("/v1/api-keys", headers=ROOT)).json()["keys"]
    assert all("hash" not in key for key in listed)
    assert listed[0]["role"] == "root"


@pytest.mark.parametrize("path", ["/v1/api-keys", "/v1/secrets", "/v1/config", "/v1/chain",
                                  "/v1/cache", "/v1/media", "/v1/logs/stream"])
async def test_client_cannot_reach_admin_routes(client, path):
    token = await create(client, "Pipeline", "client")
    assert (await client.get(path, headers=bearer(token))).status_code == 403


async def test_client_can_use_the_service(client):
    token = await create(client, "Pipeline", "client")
    for path in ("/v1/version", "/v1/jobs", "/v1/blacklist", "/v1/search"):
        assert (await client.get(path, headers=bearer(token))).status_code == 200, path


async def test_admin_manages_clients_only(client):
    admin = bearer(await create(client, "Ops", "admin"))
    assert (await client.get("/v1/secrets", headers=admin)).status_code == 200

    await create(client, "Notebook", "client", as_=admin)
    response = await client.post("/v1/api-keys", headers=admin, json={"name": "Ops 2", "role": "admin"})
    assert response.status_code == 403

    keys = {k["name"]: k for k in (await client.get("/v1/api-keys", headers=admin)).json()["keys"]}
    assert (await client.delete(f"/v1/api-keys/{keys['Ops']['id']}", headers=admin)).status_code == 403
    assert (await client.delete("/v1/api-keys/root", headers=admin)).status_code == 403
    assert (await client.post("/v1/api-keys/root/regenerate", headers=admin)).status_code == 403
    renamed = await client.patch(f"/v1/api-keys/{keys['Notebook']['id']}", headers=admin,
                                 json={"name": "Lab notebook"})
    assert renamed.json()["key"]["name"] == "Lab notebook"


async def test_revoked_key_is_rejected(client):
    token = await create(client, "Pipeline", "client")
    key_id = (await client.get("/v1/me", headers=bearer(token))).json()["id"]
    assert (await client.delete(f"/v1/api-keys/{key_id}", headers=ROOT)).status_code == 200
    assert (await client.get("/v1/me", headers=bearer(token))).status_code == 401


async def test_names_are_unique(client):
    await create(client, "Pipeline", "client")
    response = await client.post("/v1/api-keys", headers=ROOT, json={"name": " pipeline ", "role": "client"})
    assert response.status_code == 409


async def test_keys_survive_a_restart(client, tmp_path):
    token = await create(client, "Pipeline", "client")
    from scrapemm.server import auth
    auth._store = None  # As after a restart
    assert (await client.get("/v1/me", headers=bearer(token))).json()["name"] == "Pipeline"


def test_logs_do_not_show_keys(monkeypatch):
    from scrapemm.server import auth, logbuffer
    monkeypatch.setattr(auth, "_api_key", "the-root-key")
    redacted = logbuffer._redact('GET /v1/captcha/vnc?token=smm_abcdefgh123&x=1 · the-root-key · smm_zyxwvuts99')
    assert "the-root-key" not in redacted and "smm_" not in redacted and "x=1" in redacted


async def test_statistics_group_by_key(client, tmp_path):
    from scrapemm.common.wire import ResponsePayload
    from scrapemm.server.jobs import JobStore
    from scrapemm.server.retrieval_stats import retrieval_stats

    token = await create(client, "Pipeline", "client")
    pipeline = (await client.get("/v1/me", headers=bearer(token))).json()["id"]
    store = JobStore(path=tmp_path / "jobs.db")
    for key, url in ((pipeline, "https://a.example"), ("root", "https://b.example"),
                     (pipeline, "https://c.example"), ("root", "https://d.example")):
        job = store.start({"urls": [url], "api_key": key}, 1)
        store.record(job, ResponsePayload(url=url, method="stub"), False)
    # A job from before keys were recorded, as such rows are
    store._connection.execute("UPDATE jobs SET params = json_remove(params, '$.api_key') "
                              "WHERE params LIKE '%d.example%'")
    store._connection.commit()

    series = {s.get("name"): s["total"] for s in retrieval_stats(store, group="key")["series"]}
    assert series == {"Pipeline": 2, "Root": 1, None: 1}

    # Names are for those who manage the keys
    assert (await client.get("/v1/stats/retrievals?group=key", headers=bearer(token))).status_code == 403
    assert (await client.get("/v1/stats/retrievals?group=key", headers=ROOT)).status_code == 200


def test_no_job_without_an_api_key(tmp_path):
    from scrapemm.server.jobs import JobStore
    store = JobStore(path=tmp_path / "jobs.db")
    with pytest.raises(ValueError):
        store.start({"urls": ["https://a.example"]}, 1)
    with pytest.raises(ValueError):
        store.record_search("serper", 200, 10, api_key="")


async def test_searches_are_counted_under_the_callers_key(client, tmp_path, monkeypatch):
    from scrapemm.server.api import search as search_api
    from scrapemm.server.jobs import JobStore
    store = JobStore(path=tmp_path / "jobs.db")
    monkeypatch.setattr(search_api, "jobs", store)

    token = await create(client, "Notebook", "client")
    key_id = (await client.get("/v1/me", headers=bearer(token))).json()["id"]
    # Serper is not configured here: the search fails, and is counted all the same
    await client.post("/v1/search/serper", headers=bearer(token), json={"q": "scrapeMM"})
    rows = store._connection.execute("SELECT provider, api_key FROM searches").fetchall()
    assert [tuple(r) for r in rows] == [("serper", key_id)]


async def test_test_runs_run_under_the_starters_key(tmp_path, monkeypatch):
    import asyncio
    from scrapemm.common import ScrapedContent, ScrapingResponse
    from scrapemm.server import engine, jobs as jobs_module, testsuite
    from scrapemm.server.jobs import JobStore

    store = JobStore(path=tmp_path / "jobs.db")
    monkeypatch.setattr(jobs_module, "jobs", store)
    monkeypatch.setattr(testsuite, "suite", lambda: [
        {"url": "https://a.example", "category": "Open web", "expected": {}}])
    monkeypatch.setattr(testsuite, "RUNS_PATH", tmp_path / "runs.json")

    async def retrieved(url, session, **kwargs):
        return ScrapingResponse(url=url, content=ScrapedContent(html="<p>Hi</p>"),
                                method="stub", output_format="multimodal")

    monkeypatch.setattr(engine, "retrieve_one", retrieved)
    run = testsuite.TestRun()
    with pytest.raises(ValueError):
        run.start({})  # No run without a key
    status = run.start({"id": "abc123", "name": "Pipeline"})
    assert status["started_by"]["name"] == "Pipeline"
    await asyncio.wait_for(run._task, 30)
    job = store._connection.execute("SELECT params FROM jobs").fetchone()
    assert '"api_key": "abc123"' in job["params"]


@pytest.mark.parametrize("prioritize", ["completeness", "speed"])
async def test_test_runs_retrieve_with_the_chosen_priority(tmp_path, monkeypatch, prioritize):
    import asyncio
    from scrapemm.common import ScrapedContent, ScrapingResponse
    from scrapemm.server import engine, jobs as jobs_module, testsuite
    from scrapemm.server.jobs import JobStore

    monkeypatch.setattr(jobs_module, "jobs", JobStore(path=tmp_path / "jobs.db"))
    monkeypatch.setattr(testsuite, "suite", lambda: [
        {"url": "https://a.example", "category": "Open web", "expected": {}}])
    monkeypatch.setattr(testsuite, "RUNS_PATH", tmp_path / "runs.json")
    seen = []

    async def retrieved(url, session, **kwargs):
        seen.append(kwargs.get("prioritize"))
        return ScrapingResponse(url=url, content=ScrapedContent(html="<p>Hi</p>"),
                                method="stub", output_format="multimodal")

    monkeypatch.setattr(engine, "retrieve_one", retrieved)
    run = testsuite.TestRun()
    with pytest.raises(ValueError):
        run.start({"id": "abc123", "name": "Pipeline"}, prioritize="fastest")
    run.start({"id": "abc123", "name": "Pipeline"}, prioritize=prioritize)
    await asyncio.wait_for(run._task, 30)
    assert seen == [prioritize]
    assert run.report()["prioritize"] == prioritize
