"""Stopping a test run: its job is interrupted by the user, even when the run is stopped
while the job is still being opened (see `test_retrieve_disconnect`)."""

import asyncio
import time

import pytest

pytestmark = pytest.mark.server


@pytest.fixture
def run(tmp_path, monkeypatch):
    from scrapemm.server import engine, jobs as jobs_module, testsuite
    from scrapemm.server.jobs import JobStore

    store = JobStore(path=tmp_path / "jobs.db")
    monkeypatch.setattr(jobs_module, "jobs", store)
    monkeypatch.setattr(testsuite, "suite", lambda: [
        {"url": "https://a.example", "category": "Open web", "expected": {}}])
    monkeypatch.setattr(testsuite, "RUNS_PATH", tmp_path / "runs.json")

    async def never_ends(url, session, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(engine, "retrieve_one", never_ends)
    test_run = testsuite.TestRun()
    test_run.store = store
    return test_run


def rows(run) -> list[tuple]:
    return [(r["status"], r["interrupted_by"]) for r in
            run.store._connection.execute("SELECT status, interrupted_by FROM jobs")]


async def test_stopping_a_run_interrupts_its_job_by_the_user(run):
    run.start({"id": "key", "name": "Pipeline"})
    await asyncio.sleep(0.3)
    assert rows(run) == [("running", None)]
    await run.cancel()
    assert rows(run) == [("interrupted", "user")]
    assert run.report()["state"] == "cancelled"


async def test_stopping_a_run_while_its_job_is_being_opened_still_closes_the_job(run, monkeypatch):
    real_start = run.store.start

    def slow_start(*args, **kwargs):
        time.sleep(0.5)  # In the worker thread, as under load
        return real_start(*args, **kwargs)

    monkeypatch.setattr(run.store, "start", slow_start)
    run.start({"id": "key", "name": "Pipeline"})
    await asyncio.sleep(0.05)
    await run.cancel()
    await asyncio.sleep(1)
    assert rows(run) == [("interrupted", "user")]


async def test_a_run_stopped_by_a_server_shutdown_says_so(run, monkeypatch):
    from scrapemm.server import interrupts
    run.start({"id": "key", "name": "Pipeline"})
    await asyncio.sleep(0.3)
    monkeypatch.setattr(interrupts, "_shutting_down", True)
    run._task.cancel()  # Not by a user: the server's shutdown does that
    await asyncio.sleep(0.3)
    assert rows(run) == [("interrupted", "server")]
