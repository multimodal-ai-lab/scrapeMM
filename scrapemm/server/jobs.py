"""Job history: what was retrieved, when, how it went, and what came back.

Every `/v1/retrieve` call becomes a job, and every URL in it a result row. The web UI
browses both, which is the difference between "that batch had some failures" and being
able to look at exactly which URL failed with which error three days ago.

SQLite because the server is a single process by design (the headed browser holds an
exclusive profile lock, so it could not be otherwise) and the volumes involved are a
research lab's, not a search engine's.

Retention prunes *job records* only. Media files are never touched here: a client that
ran in `link` mode holds references straight into the registry, and deleting those
files would break sequences that were handed out long ago.
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

from scrapemm.common.paths import APP_NAME
from scrapemm.common.wire import ResponsePayload
from .paths import JOBS_DB_PATH

logger = logging.getLogger(APP_NAME)

# Content is stored so the UI can show what a job produced. A page's HTML can be large,
# and a job of 500 URLs would otherwise bloat the database out of proportion to its
# usefulness, so each field is capped and the UI says when it truncated something.
MAX_STORED_CONTENT = 256 * 1024

DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_JOBS = 10_000


class JobStore:
    """The job history. Thread-safe: the retrieval routines write from the event loop
    while the UI reads from request handlers."""

    def __init__(self, path=JOBS_DB_PATH):
        self.path = path
        self._lock = threading.RLock()
        # Bumped on every write, so the live dashboard re-queries only after a change
        self.version = 0
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL;")
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id           TEXT PRIMARY KEY,
                    created_at   REAL NOT NULL,
                    finished_at  REAL,
                    status       TEXT NOT NULL,
                    params       TEXT NOT NULL,
                    url_count    INTEGER NOT NULL DEFAULT 0,
                    succeeded    INTEGER NOT NULL DEFAULT 0,
                    failed       INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS results (
                    job_id       TEXT NOT NULL,
                    url          TEXT NOT NULL,
                    success      INTEGER NOT NULL,
                    method       TEXT,
                    errors       TEXT,
                    content      TEXT,
                    retrieval_time REAL,
                    from_cache   INTEGER NOT NULL DEFAULT 0,
                    created_at   REAL NOT NULL,
                    PRIMARY KEY (job_id, url)
                );
                CREATE INDEX IF NOT EXISTS jobs_created_idx ON jobs(created_at DESC);
                CREATE INDEX IF NOT EXISTS results_url_idx ON results(url);
                -- The dashboard asks for the most recent N results and for a count
                -- since a timestamp on every load; without this both degrade into a
                -- full scan of a table that only ever grows.
                CREATE INDEX IF NOT EXISTS results_created_idx ON results(created_at DESC);
                CREATE INDEX IF NOT EXISTS results_method_idx ON results(method)
                    WHERE success = 1;
                """
            )
            self._connection.commit()
            self.version += 1

    # --- Writing ------------------------------------------------------------------

    def start(self, params: dict, url_count: int) -> str:
        # Ten hex characters: short enough to read and quote in full, and still 40 bits
        # of entropy, which is far more than a job history of a few thousand rows needs.
        # The column is a primary key, so the vanishing chance of a clash surfaces as an
        # error rather than as two jobs quietly sharing an id.
        job_id = uuid.uuid4().hex[:10]
        with self._lock:
            self._connection.execute(
                "INSERT INTO jobs (id, created_at, status, params, url_count) "
                "VALUES (?, ?, 'running', ?, ?)",
                (job_id, time.time(), json.dumps(params, default=str), url_count))
            self._connection.commit()
            self.version += 1
        return job_id

    def record(self, job_id: str, payload: ResponsePayload, success: bool) -> None:
        content = None
        if payload.content is not None:
            content = json.dumps(_truncate(payload.content.to_dict()))
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO results (job_id, url, success, method, errors, "
                "content, retrieval_time, from_cache, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, payload.url, int(success), payload.method,
                 json.dumps(payload.errors), content, payload.retrieval_time,
                 int(payload.from_cache), time.time()))
            self._connection.commit()
            self.version += 1

    def finish(self, job_id: str, succeeded: int, failed: int,
               status: str = "completed") -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE jobs SET finished_at = ?, status = ?, succeeded = ?, failed = ? "
                "WHERE id = ?",
                (time.time(), status, succeeded, failed, job_id))
            self._connection.commit()
            self.version += 1

    def interrupt_unfinished(self) -> int:
        """Marks the jobs a previous server process left running. Called at startup:
        nothing can still be working on them, and they would otherwise show as running
        forever."""
        with self._lock:
            count = self._connection.execute(
                "UPDATE jobs SET status = 'interrupted', finished_at = ? "
                "WHERE status = 'running'", (time.time(),)).rowcount
            self._connection.commit()
            self.version += 1
        return count

    # --- Reading ------------------------------------------------------------------

    def _filters(self, url: Optional[str], status: Optional[str],
                 output_format: Optional[str], method: Optional[str],
                 success: Optional[bool], since: Optional[float],
                 until: Optional[float]) -> tuple[str, list[Any]]:
        """Builds the WHERE clause the list and count queries share.

        The per-URL criteria (url, method, success) match a job if *any* of its results
        does, which is what somebody looking for "the job where example.com failed"
        actually means.
        """
        clauses: list[str] = []
        args: list[Any] = []

        if status:
            clauses.append("jobs.status = ?")
            args.append(status)
        if output_format:
            # The parameters are stored as JSON; this is a substring match on the one
            # key, which beats adding a column for something so rarely filtered.
            clauses.append("jobs.params LIKE ?")
            args.append(f'%"output_format": "{output_format}"%')
        if since is not None:
            clauses.append("jobs.created_at >= ?")
            args.append(since)
        if until is not None:
            clauses.append("jobs.created_at <= ?")
            args.append(until)

        result_clauses: list[str] = []
        if url:
            result_clauses.append("results.url LIKE ?")
            args.append(f"%{url}%")
        if method:
            result_clauses.append("results.method = ?")
            args.append(method)
        if success is not None:
            result_clauses.append("results.success = ?")
            args.append(int(success))
        if result_clauses:
            clauses.append(
                "EXISTS (SELECT 1 FROM results WHERE results.job_id = jobs.id AND "
                + " AND ".join(result_clauses) + ")")

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, args

    def list_jobs(self, limit: int = 50, offset: int = 0,
                  status: Optional[str] = None, url: Optional[str] = None,
                  output_format: Optional[str] = None, method: Optional[str] = None,
                  success: Optional[bool] = None, since: Optional[float] = None,
                  until: Optional[float] = None, sort: str = "newest") -> list[dict]:
        where, args = self._filters(url, status, output_format, method, success,
                                    since, until)
        order = SORTS.get(sort, SORTS["newest"])
        query = f"SELECT * FROM jobs{where} ORDER BY {order}, created_at DESC LIMIT ? OFFSET ?"
        with self._lock:
            # `now` stands in for the end of jobs still running, when sorting by duration
            rows = self._connection.execute(
                query.replace(":now", str(time.time())), [*args, limit, offset]).fetchall()
            jobs = [_job_row(row) for row in rows]
            # Every listed job's URLs in one query, rather than one query per job
            ids = [job["id"] for job in jobs]
            by_job: dict[str, list[sqlite3.Row]] = {job_id: [] for job_id in ids}
            if ids:
                for row in self._connection.execute(
                        "SELECT job_id, url, success, method, retrieval_time, from_cache, "
                        f"errors FROM results WHERE job_id IN ({','.join('?' * len(ids))}) "
                        "ORDER BY created_at", ids):
                    by_job[row["job_id"]].append(row)

        for job in jobs:
            _summarize(job, by_job[job["id"]])
        return jobs

    def count_jobs(self, status: Optional[str] = None, url: Optional[str] = None,
                   output_format: Optional[str] = None, method: Optional[str] = None,
                   success: Optional[bool] = None, since: Optional[float] = None,
                   until: Optional[float] = None) -> int:
        where, args = self._filters(url, status, output_format, method, success,
                                    since, until)
        with self._lock:
            return self._connection.execute(
                f"SELECT COUNT(*) FROM jobs{where}", args).fetchone()[0]

    def known_methods(self) -> list[str]:
        """Every method that has ever produced a result here, for the filter dropdown."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT DISTINCT method FROM results WHERE method IS NOT NULL "
                "ORDER BY method").fetchall()
        return [row["method"] for row in rows]

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            results = self._connection.execute(
                "SELECT * FROM results WHERE job_id = ? ORDER BY created_at",
                (job_id,)).fetchall()
        job = _job_row(row)
        job["results"] = [_result_row(r) for r in results]
        job["duration"] = _duration(job)
        # Live counts: the stored ones are only written once the job finishes, so a
        # running job would otherwise show none of what it has done so far
        done = {r["url"] for r in job["results"]}
        job["succeeded"] = sum(1 for r in job["results"] if r["success"])
        job["failed"] = len(job["results"]) - job["succeeded"]
        requested = list(dict.fromkeys(job["params"].get("urls") or [])) or list(done)
        job["pending"] = [url for url in requested if url not in done]
        return job

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._connection.execute("DELETE FROM results WHERE job_id = ?", (job_id,))
            self._connection.commit()
            self.version += 1
        return cursor.rowcount > 0

    # --- Housekeeping -------------------------------------------------------------

    def prune(self, retention_days: float = DEFAULT_RETENTION_DAYS,
              max_jobs: int = DEFAULT_MAX_JOBS) -> int:
        """Drops job records that are too old or too many. Never touches media."""
        deadline = time.time() - retention_days * 24 * 60 * 60
        with self._lock:
            removed = self._connection.execute(
                "DELETE FROM jobs WHERE created_at < ?", (deadline,)).rowcount
            removed += self._connection.execute(
                "DELETE FROM jobs WHERE id NOT IN ("
                "  SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)",
                (max_jobs,)).rowcount
            self._connection.execute(
                "DELETE FROM results WHERE job_id NOT IN (SELECT id FROM jobs)")
            self._connection.commit()
            self.version += 1
        if removed:
            logger.info(f"Pruned {removed} job records (media untouched).")
        return removed

    def stats(self) -> dict:
        with self._lock:
            jobs = self._connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            # One pass over the results for all three figures
            results, succeeded, cached = self._connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(success), 0), COALESCE(SUM(from_cache), 0) "
                "FROM results").fetchone()
        return {"jobs": jobs, "urls": results, "succeeded": succeeded,
                "failed": results - succeeded, "from_cache": cached,
                "cache_hit_rate": cached / results if results else None}

    def method_counts(self) -> dict[str, int]:
        """How many URLs each method has successfully retrieved. Only successes count:
        a method that was tried and failed did not retrieve anything, and the number is
        meant to show what a method is actually carrying."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT method, COUNT(*) AS n FROM results "
                "WHERE success = 1 AND method IS NOT NULL GROUP BY method").fetchall()
        return {row["method"]: row["n"] for row in rows}

    def recent_success_rate(self, limit: int = 1000) -> dict:
        """The share of retrievals that succeeded, over the most recent `limit` of them.

        Recent rather than all-time on purpose: a server that has been running for
        months would otherwise average away exactly the degradation this number exists
        to surface.
        """
        with self._lock:
            rows = self._connection.execute(
                "SELECT success FROM results ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        total = len(rows)
        succeeded = sum(1 for row in rows if row["success"])
        return {
            "window": limit,
            "total": total,
            "succeeded": succeeded,
            "failed": total - succeeded,
            # None rather than 100% when nothing has run: a fresh server has no rate,
            # and showing a perfect one would be a lie of omission.
            "rate": (succeeded / total) if total else None,
        }

    def count_since(self, seconds: float) -> int:
        """How many URLs were retrieved in the last `seconds`."""
        with self._lock:
            return self._connection.execute(
                "SELECT COUNT(*) FROM results WHERE created_at >= ?",
                (time.time() - seconds,)).fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _truncate(content: dict) -> dict:
    """Caps the stored text fields, marking what was cut so the UI can say so."""
    content = dict(content)
    content["truncated"] = []
    for field in ("html", "markdown", "multimodal"):
        value = content.get(field)
        if isinstance(value, str) and len(value) > MAX_STORED_CONTENT:
            content[field] = value[:MAX_STORED_CONTENT]
            content["truncated"].append(field)
    return content


# How the job list can be ordered. Fixed SQL per key, so no input ever reaches the query.
SORTS = {
    "newest": "jobs.created_at DESC",
    "oldest": "jobs.created_at ASC",
    "longest": "(COALESCE(jobs.finished_at, :now) - jobs.created_at) DESC",
    "shortest": "(COALESCE(jobs.finished_at, :now) - jobs.created_at) ASC",
    "most_urls": "jobs.url_count DESC",
    "fewest_urls": "jobs.url_count ASC",
    "most_failed": "jobs.failed DESC",
}

# URLs listed per job in the overview; the detail view has them all
URL_PREVIEW = 5

# When a URL failed on several methods, the error that says most about why. A CAPTCHA or
# a missing target explains the whole failure; "method X returned nothing" rarely does.
ERROR_PRIORITY = ["DomainBlacklistedError", "CaptchaEncounteredError", "TargetUnavailableError",
                  "AccessBlockedError", "RateLimitError", "QuotaExceededError",
                  "UnsupportedDomainError", "TimeoutError", "RetrievalFailed"]


def _summarize(job: dict, results: list[sqlite3.Row]) -> None:
    """Adds what the overview shows of a job: its first URLs in the order they were
    requested -- with the result of each that is done, so a running job lists the rest
    as pending -- plus its counts and timings."""
    done = {row["url"]: row for row in results}
    requested = list(dict.fromkeys(job["params"].get("urls") or [])) or list(done)

    job["urls"] = []
    for url in requested[:URL_PREVIEW]:
        row = done.get(url)
        if row is None:
            job["urls"].append({"url": url, "state": "pending"})
            continue
        entry = {"url": url, "state": "ok" if row["success"] else "failed",
                 "method": row["method"], "retrieval_time": row["retrieval_time"],
                 "from_cache": bool(row["from_cache"])}
        if not row["success"]:
            entry["error"] = _main_error(json.loads(row["errors"]) if row["errors"] else {})
        job["urls"].append(entry)

    # Live counts: the stored ones are only written once the job finishes
    job["done"] = len(done)
    job["succeeded"] = sum(1 for row in results if row["success"])
    job["failed"] = len(done) - job["succeeded"]
    job["duration"] = _duration(job)
    job["methods"] = sorted({row["method"] for row in results if row["method"]})
    # The full list can be long and is on the detail page; only its size matters here
    job["params"] = {k: v for k, v in job["params"].items() if k != "urls"}


def _duration(job: dict) -> float:
    """How long the job took, start to finish -- or so far, while it runs. Not the sum
    of its URLs' retrieval times: those run concurrently and would add up to far more."""
    return (job["finished_at"] or time.time()) - job["created_at"]


def _main_error(errors: dict) -> Optional[dict]:
    """The most telling of a URL's errors, as {type, message}."""
    if not errors:
        return None
    ranked = sorted(errors.values(), key=lambda e: ERROR_PRIORITY.index(e.get("type"))
                    if e.get("type") in ERROR_PRIORITY else len(ERROR_PRIORITY))
    error = ranked[0]
    return {"type": error.get("type"), "message": (error.get("message") or "")[:300]}


def _job_row(row: sqlite3.Row) -> dict:
    job = dict(row)
    job["params"] = json.loads(job["params"]) if job["params"] else {}
    return job


def _result_row(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["errors"] = json.loads(result["errors"]) if result["errors"] else {}
    result["content"] = json.loads(result["content"]) if result["content"] else None
    result["success"] = bool(result["success"])
    result["from_cache"] = bool(result["from_cache"])
    return result


jobs = JobStore()
