"""A client that goes away must not leave its job "running" for good, and a job that is
interrupted says by whom: the client that left, the user, or the server."""

import asyncio
import json
import time

import pytest

pytestmark = pytest.mark.server


@pytest.fixture
def stream(tmp_path, monkeypatch):
    """Runs the retrieval stream of a one-URL request against a job store of its own, with
    a retrieval that never ends. Returns a function: given when the client disconnects (a
    negative time: never) and how long the job's write takes, it returns the store, the
    stream's task and what was cancelled. `run.sent` has the messages the client got."""
    from starlette.responses import StreamingResponse
    from scrapemm.server.api import retrieve as api
    from scrapemm.server.auth import Principal
    from scrapemm.server.jobs import JobStore

    store = JobStore(path=tmp_path / "jobs.db")
    monkeypatch.setattr(api, "jobs", store)
    monkeypatch.setattr(api, "HEARTBEAT_INTERVAL", 0.2)
    cancelled = []
    sent = []

    async def never_ends(url, session, **kwargs):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(url)
            raise

    monkeypatch.setattr(api, "retrieve_one", never_ends)

    async def run(disconnect_after: float, write_takes: float = 0.0):
        if write_takes:
            real_start = store.start

            def slow_start(*args, **kwargs):
                time.sleep(write_takes)  # In the worker thread, as under load
                return real_start(*args, **kwargs)

            monkeypatch.setattr(store, "start", slow_start)
        request = api.RetrieveRequest(urls=["https://example.org/a"])
        response = StreamingResponse(api._stream(request, Principal(id="key", name="n", role="user")))
        gone = asyncio.Event()

        async def receive():
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "method": "POST", "path": "/", "headers": []}
        task = asyncio.create_task(response(scope, receive, send))
        await asyncio.sleep(max(disconnect_after, 0))
        if disconnect_after >= 0:
            gone.set()
        await asyncio.sleep(write_takes + 0.5)
        return store, task, cancelled

    run.sent = sent
    run.store = store
    return run


def statuses(store) -> list[str]:
    return [row["status"] for row in store._connection.execute("SELECT status FROM jobs")]


def interrupters(store) -> list[str]:
    return [row["interrupted_by"] for row in store._connection.execute("SELECT interrupted_by FROM jobs")]


def messages(sent) -> list[dict]:
    return [json.loads(m["body"]) for m in sent if m["type"] == "http.response.body" and m.get("body")]


async def test_job_is_interrupted_when_the_client_goes_away_mid_batch(stream):
    store, task, cancelled = await stream(disconnect_after=0.5)
    assert statuses(store) == ["interrupted"]
    assert interrupters(store) == ["client"]
    assert cancelled == ["https://example.org/a"]  # Nobody is left to receive it: not scraped on
    assert task.done()


async def test_job_is_interrupted_when_the_client_goes_away_while_it_is_being_opened(stream):
    """The write of the job runs in a thread and outlives the cancelled stream: found as
    jobs that stayed "running" for hours, created in the very millisecond their clients
    disconnected."""
    store, task, cancelled = await stream(disconnect_after=0.05, write_takes=0.5)
    assert statuses(store) == ["interrupted"]
    assert interrupters(store) == ["client"]
    assert cancelled == []  # The retrieval never started


async def test_job_is_interrupted_by_the_server_when_it_is_stopping(stream, monkeypatch):
    from scrapemm.server import interrupts
    monkeypatch.setattr(interrupts, "_shutting_down", True)
    store, task, cancelled = await stream(disconnect_after=0.5)
    assert statuses(store) == ["interrupted"]
    assert interrupters(store) == ["server"]


async def test_a_user_can_interrupt_a_running_job(stream):
    """The client stays connected: it is told how the job ended, and the job says by whom."""
    from scrapemm.server import interrupts
    run = asyncio.create_task(stream(disconnect_after=-1))  # Never disconnects
    await asyncio.sleep(0.3)
    job_id = stream.store._connection.execute("SELECT id FROM jobs").fetchone()["id"]
    assert interrupts.interrupt(job_id)
    store, task, cancelled = await run
    assert statuses(store) == ["interrupted"]
    assert interrupters(store) == ["user"]
    assert cancelled == ["https://example.org/a"]
    summary = messages(stream.sent)[-1]
    assert summary["type"] == "summary" and summary["interrupted_by"] == "user"
    assert not interrupts.interrupt(job_id)  # Nothing is left to interrupt
