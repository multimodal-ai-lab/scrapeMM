"""A client that goes away must not leave its job "running" for good."""

import asyncio
import time

import pytest

pytestmark = pytest.mark.server


@pytest.fixture
def stream(tmp_path, monkeypatch):
    """Runs the retrieval stream of a one-URL request against a job store of its own, with
    a retrieval that never ends. Returns a function: given when the client disconnects and
    how long the job's write takes, it returns the store, the stream's task and what was
    cancelled."""
    from starlette.responses import StreamingResponse
    from scrapemm.server.api import retrieve as api
    from scrapemm.server.auth import Principal
    from scrapemm.server.jobs import JobStore

    store = JobStore(path=tmp_path / "jobs.db")
    monkeypatch.setattr(api, "jobs", store)
    monkeypatch.setattr(api, "HEARTBEAT_INTERVAL", 0.2)
    cancelled = []

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
            pass

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "method": "POST", "path": "/", "headers": []}
        task = asyncio.create_task(response(scope, receive, send))
        await asyncio.sleep(disconnect_after)
        gone.set()
        await asyncio.sleep(write_takes + 0.5)
        return store, task, cancelled

    return run


def statuses(store) -> list[str]:
    return [row["status"] for row in store._connection.execute("SELECT status FROM jobs")]


async def test_job_is_interrupted_when_the_client_goes_away_mid_batch(stream):
    store, task, cancelled = await stream(disconnect_after=0.5)
    assert statuses(store) == ["interrupted"]
    assert cancelled == ["https://example.org/a"]  # Nobody is left to receive it: not scraped on
    assert task.done()


async def test_job_is_interrupted_when_the_client_goes_away_while_it_is_being_opened(stream):
    """The write of the job runs in a thread and outlives the cancelled stream: found as
    jobs that stayed "running" for hours, created in the very millisecond their clients
    disconnected."""
    store, task, cancelled = await stream(disconnect_after=0.05, write_takes=0.5)
    assert statuses(store) == ["interrupted"]
    assert cancelled == []  # The retrieval never started
