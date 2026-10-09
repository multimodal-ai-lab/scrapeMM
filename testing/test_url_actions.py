"""Stopping one URL of a running job, and retrying a URL of a finished one."""

import asyncio
import json

import pytest

pytestmark = pytest.mark.server


@pytest.fixture
def served(tmp_path, monkeypatch):
    """The retrieval stream against a job store of its own. URLs with "slow" in them never
    finish; the others finish at once. Returns the store and a function that starts a
    request of those URLs, answering with the task that streams it."""
    from starlette.responses import StreamingResponse
    from scrapemm.common import ScrapedContent, ScrapingResponse
    from scrapemm.server.api import retrieve as api
    from scrapemm.server.auth import Principal
    from scrapemm.server.jobs import JobStore

    store = JobStore(path=tmp_path / "jobs.db")
    monkeypatch.setattr(api, "jobs", store)
    monkeypatch.setattr(api, "HEARTBEAT_INTERVAL", 0.2)
    calls: list[tuple[str, dict]] = []

    async def retrieve_one(url, session, **kwargs):
        calls.append((url, kwargs))
        if "slow" in url:
            await asyncio.sleep(3600)
        return ScrapingResponse(url=url, content=ScrapedContent(markdown="# Hi"),
                                method="stub", output_format=kwargs["output_format"])

    monkeypatch.setattr(api, "retrieve_one", retrieve_one)
    sent: list[dict] = []

    def start(urls: list[str]) -> asyncio.Task:
        request = api.RetrieveRequest(urls=urls, output_format="markdown")
        response = StreamingResponse(api._stream(request, Principal(id="key", name="n", role="user")))
        gone = asyncio.Event()

        async def receive():
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "method": "POST", "path": "/", "headers": []}
        return asyncio.create_task(response(scope, receive, send))

    start.store, start.sent, start.calls, start.api, start.principal = store, sent, calls, api, Principal
    return start


def job_id(store) -> str:
    return store._connection.execute("SELECT id FROM jobs").fetchone()["id"]


async def test_stopping_one_url_leaves_the_job_going_on_with_the_others(served):
    from scrapemm.server import interrupts
    task = served(["https://example.org/slow", "https://example.org/fast"])
    await asyncio.sleep(0.5)
    store = served.store
    assert interrupts.interrupt(job_id(store), "https://example.org/slow")
    await asyncio.wait_for(task, 5)

    job = store.get_job(job_id(store))
    assert job["status"] == "completed" and job["interrupted_by"] is None
    results = {r["url"]: r for r in job["results"]}
    assert results["https://example.org/fast"]["success"]
    stopped = results["https://example.org/slow"]
    assert not stopped["success"]
    assert stopped["errors"]["scrapemm"]["type"] == "RetrievalInterrupted"
    # The client got the stopped URL's result as well as the other one's
    lines = [json.loads(m["body"]) for m in served.sent if m["type"] == "http.response.body" and m.get("body")]
    assert {m["payload"]["url"] for m in lines if m["type"] == "result"} == set(results)


async def test_only_a_url_being_retrieved_can_be_stopped(served):
    from scrapemm.server import interrupts
    task = served(["https://example.org/slow", "https://example.org/fast"])
    await asyncio.sleep(0.5)
    store = served.store
    assert not interrupts.interrupt(job_id(store), "https://example.org/fast")  # Done already
    assert not interrupts.interrupt(job_id(store), "https://example.org/other")  # Not in the job
    assert interrupts.interrupt(job_id(store))  # The whole job, as before
    await asyncio.wait_for(task, 5)


async def test_a_job_in_the_background_records_its_results_and_can_be_interrupted(served):
    from scrapemm.server import interrupts
    request = served.api.RetrieveRequest(urls=["https://example.org/slow"], use_cache=False,
                                          output_format="markdown")
    new_id = await served.api.start_in_background(
        request, served.principal(id="key", name="n", role="user"))
    store = served.store
    assert store.get_job(new_id)["status"] == "running"
    assert interrupts.interrupt(new_id)
    await asyncio.sleep(0.5)
    job = store.get_job(new_id)
    assert job["status"] == "interrupted" and job["interrupted_by"] == "user"
