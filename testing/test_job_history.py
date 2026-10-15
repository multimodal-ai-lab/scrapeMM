"""The job history: all-time counters, and limits from the settings (`job_retention_days`,
`max_jobs`; 0 for no limit) on how long jobs keep their content. The jobs are kept."""

import time

import pytest

from scrapemm.common.wire import ContentPayload, ResponsePayload
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
            store.record(job, ResponsePayload(url=f"https://e.example/{i}/{u}", method="stub",
                                              content=ContentPayload(markdown="Text")), True)
        ids.append(job)
    return ids


def test_pruning_drops_only_the_content(store):
    """Jobs beyond the limit lose their content, but they and their results stay: the
    statistics are made of them."""
    ids = run_jobs(store, 5)
    assert store.prune(max_jobs=2) == 6  # Three jobs of two URLs
    stats = store.stats()
    assert (stats["jobs"], stats["urls"]) == (5, 10)
    assert (stats["all_time"]["jobs"], stats["all_time"]["urls"]) == (5, 10)
    old, new = store.get_job(ids[0]), store.get_job(ids[-1])
    assert [r["method"] for r in old["results"]] == ["stub", "stub"]
    assert not any(r["has_content"] for r in old["results"])
    assert store.get_content(ids[0], "https://e.example/0/0") is None
    assert all(r["has_content"] for r in new["results"])
    assert store.prune(max_jobs=2) == 0  # Nothing left to drop


def test_a_url_recorded_again_counts_once(store):
    job = run_jobs(store, 1, urls_each=1)[0]
    store.record(job, ResponsePayload(url="https://e.example/0/0", method="stub"), True)
    assert store.stats()["all_time"]["urls"] == 1


def test_limits_come_from_the_settings(store, monkeypatch):
    run_jobs(store, 4)
    monkeypatch.setattr(config, "_config", {"max_jobs": 3})
    assert store.prune() == 2  # One job's two results
    monkeypatch.setattr(config, "_config", {"max_jobs": 0, "job_retention_days": 0})
    assert store.prune() == 0  # 0: no limit
    # Too old for a retention of a day
    store._connection.execute("UPDATE jobs SET created_at = ?", (time.time() - 2 * 86400,))
    store._connection.commit()
    monkeypatch.setattr(config, "_config", {"job_retention_days": 1})
    assert store.prune() == 6  # The other three jobs' results
    assert store.stats()["jobs"] == 4


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


def test_jobs_found_running_at_startup_were_interrupted_by_the_server(tmp_path):
    from scrapemm.server.jobs import JobStore
    store = JobStore(path=tmp_path / "jobs.db")
    left_running = store.start({"urls": ["https://a.example"], "api_key": "k"}, 1)
    done = store.start({"urls": ["https://b.example"], "api_key": "k"}, 1)
    store.finish(done, 1, 0)
    assert store.interrupt_unfinished() == 1
    assert store.get_job(left_running)["interrupted_by"] == "server"
    assert store.get_job(done)["interrupted_by"] is None


def test_a_job_interrupted_says_by_whom(tmp_path):
    from scrapemm.server.jobs import JobStore
    store = JobStore(path=tmp_path / "jobs.db")
    by_user = store.start({"urls": ["https://a.example"], "api_key": "k"}, 1)
    store.finish(by_user, 0, 0, status="interrupted", interrupted_by="user")
    finished = store.start({"urls": ["https://b.example"], "api_key": "k"}, 1)
    store.finish(finished, 1, 0, status="completed", interrupted_by="user")  # Only if interrupted
    assert store.get_job(by_user)["interrupted_by"] == "user"
    assert store.get_job(finished)["interrupted_by"] is None
    assert store.interrupt_stale(by_user, "user") is False  # No longer running


def test_jobs_sort_by_failures_both_ways(store):
    """Each sort property goes both ways (the Jobs view picks property and order apart)."""
    ids = run_jobs(store, 3)
    for i, job in enumerate(ids):
        store._connection.execute("UPDATE jobs SET failed = ? WHERE id = ?", (i, job))
    store._connection.commit()
    most = [j["id"] for j in store.list_jobs(sort="most_failed")]
    fewest = [j["id"] for j in store.list_jobs(sort="fewest_failed")]
    assert most == ids[::-1] and fewest == ids


def test_retrievals_list_filter_and_sort(store):
    """The History's Retrievals view: one entry per URL of every job, filtered like the
    jobs and sorted by when or how long."""
    job = store.start({"urls": ["https://a.example", "https://b.example"], "api_key": "root",
                       "output_format": "markdown"}, 2)
    store.record(job, ResponsePayload(url="https://a.example", method="stub", retrieval_time=3.0), True)
    store.record(job, ResponsePayload(url="https://b.example", retrieval_time=9.0,
                                      errors={"stub": {"type": "TimeoutError", "message": "slow"}}), False)
    assert [r["url"] for r in store.list_results(sort="fastest")] == ["https://a.example", "https://b.example"]
    failed = store.list_results(outcome="error")
    assert [r["url"] for r in failed] == ["https://b.example"]
    assert failed[0]["error"]["type"] == "TimeoutError" and failed[0]["output_format"] == "markdown"
    assert store.count_results(output_format="markdown") == 2
    assert store.count_results(output_format="html") == 0
    assert store.count_results(url="a.example") == 1


def test_searches_are_recorded_for_the_history(store):
    store.record_search("serper", 200, 2, "root", {"q": "cats"}, 'web search "cats"', None,
                        0.5, ["https://x.example", "https://y.example"])
    store.record_search("serper", 429, None, "root", {"q": "dogs"}, 'web search "dogs"',
                        "Rate-limited", 0.1)
    store.record_search("serper", 200, 5, "root")  # As recorded before queries were
    assert [s["summary"] for s in store.list_searches(sort="most_results")][:2] == [None, 'web search "cats"']
    cats = store.list_searches(query="cats")
    assert cats[0]["urls"] == ["https://x.example", "https://y.example"]
    assert cats[0]["query"] == {"q": "cats"}
    assert store.count_searches(status="failed") == store.count_searches(status="429") == 1
    assert store.search_stats() == {"searches": 3, "failed": 1}
    assert store.search_providers() == ["serper"]
