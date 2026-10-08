"""The job history: all-time counters that pruning never lowers, and limits from the
settings (`job_retention_days`, `max_jobs`; 0 for no limit)."""

import time

import pytest

from scrapemm.common.wire import ResponsePayload
from scrapemm.server import config
from scrapemm.server.jobs import JobStore

pytestmark = pytest.mark.server


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_config", {})
    return JobStore(path=tmp_path / "jobs.db")


def run_jobs(store: JobStore, n: int, urls_each: int = 2) -> list[str]:
    ids = []
    for i in range(n):
        job = store.start({"urls": [f"https://e.example/{i}/{u}" for u in range(urls_each)],
                           "api_key": "root"}, urls_each)
        for u in range(urls_each):
            store.record(job, ResponsePayload(url=f"https://e.example/{i}/{u}", method="stub"), True)
        ids.append(job)
    return ids


def test_all_time_counts_survive_pruning(store):
    run_jobs(store, 5)
    assert store.prune(max_jobs=2) == 3
    stats = store.stats()
    assert (stats["jobs"], stats["urls"]) == (2, 4)  # What the history still holds
    assert (stats["all_time"]["jobs"], stats["all_time"]["urls"]) == (5, 10)


def test_a_url_recorded_again_counts_once(store):
    job = run_jobs(store, 1, urls_each=1)[0]
    store.record(job, ResponsePayload(url="https://e.example/0/0", method="stub"), True)
    assert store.stats()["all_time"]["urls"] == 1


def test_limits_come_from_the_settings(store, monkeypatch):
    run_jobs(store, 4)
    monkeypatch.setattr(config, "_config", {"max_jobs": 3})
    assert store.prune() == 1
    monkeypatch.setattr(config, "_config", {"max_jobs": 0, "job_retention_days": 0})
    assert store.prune() == 0  # 0: no limit
    # Too old for a retention of a day
    store._connection.execute("UPDATE jobs SET created_at = ?", (time.time() - 2 * 86400,))
    store._connection.commit()
    monkeypatch.setattr(config, "_config", {"job_retention_days": 1})
    assert store.prune() == 3


def test_counters_start_from_an_existing_history(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_config", {})
    path = tmp_path / "jobs.db"
    store = JobStore(path=path)
    run_jobs(store, 3)
    store._connection.execute("DROP TABLE counters")  # As a database from before them
    store._connection.commit()
    store.close()
    reopened = JobStore(path=path)
    assert reopened.stats()["all_time"]["jobs"] == 3
