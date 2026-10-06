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

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

from scrapemm.common.outcome import OK, UNAVAILABLE, ERROR, classify, decisive_error
from scrapemm.common.paths import APP_NAME
from scrapemm.common.wire import ResponsePayload
from .paths import JOBS_DB_PATH

logger = logging.getLogger(APP_NAME)

# Content is stored so the UI can show what a job produced. A page's HTML can be large,
# and a job of 500 URLs would otherwise bloat the database out of proportion to its
# usefulness, so each field is capped and the UI says when it truncated something.
MAX_STORED_CONTENT = 256 * 1024

RESULTS_COLUMNS = """
    job_id         TEXT NOT NULL,
    url            TEXT NOT NULL,
    success        INTEGER NOT NULL,
    method         TEXT,
    errors         TEXT,
    retrieval_time REAL,
    queue_time     REAL,
    from_cache     INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    outcome        TEXT,
    outcome_kind   TEXT,
    PRIMARY KEY (job_id, url)
"""

RESULTS_INDEXES = """
    CREATE INDEX IF NOT EXISTS results_url_idx ON results(url);
    -- The dashboard asks for the most recent N results and for a count since a
    -- timestamp on every load; without this both degrade into a full scan of a table
    -- that only ever grows.
    CREATE INDEX IF NOT EXISTS results_created_idx ON results(created_at DESC);
    CREATE INDEX IF NOT EXISTS results_method_idx ON results(method) WHERE success = 1;
    CREATE INDEX IF NOT EXISTS results_outcome_idx ON results(outcome, outcome_kind);
    -- Covers every per-URL job filter (see `_filters`), so they never read the rows
    CREATE INDEX IF NOT EXISTS results_filter_idx
        ON results(job_id, url, success, method, outcome, outcome_kind);
"""

WAL_SIZE_LIMIT = 64 * 1024 * 1024  # Bytes the write-ahead log is cut back to

# The dashboard's "today" figures: counted per slot of 15 minutes over the last day and
# a bit, see `recent_counts()`
SLOT = 15 * 60
RECENT_SPAN = 25 * 60 * 60

DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_JOBS = 10_000


class JobStore:
    """The job history. Thread-safe, and meant to be called from worker threads
    (`asyncio.to_thread`) rather than on the event loop: a query on the loop stalls
    every request the server has in flight.

    Two connections, each behind its own lock: one writes, one reads. In WAL mode SQLite
    lets a reader and a writer work at once, so a long query of the UI never holds up
    the recording of results, nor the other way round.

    Page content lives in a table of its own (`result_content`). In the results rows, it
    made every row that was read -- to count outcomes, say -- walk the content's
    overflow pages: on a 4 GB history, the dashboard's all-time figures took a second
    per poll, and a filtered job count 80 s."""

    def __init__(self, path=JOBS_DB_PATH):
        self.path = path
        self._lock = threading.RLock()  # The write connection's
        self._read_lock = threading.RLock()
        # Bumped on every write, so the live dashboard re-queries only after a change
        self.version = 0
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL;")
        # The write-ahead log otherwise keeps the size of the largest transaction ever
        # written, for good: 2 GB on production, after the outcome columns were filled in
        self._connection.execute(f"PRAGMA journal_size_limit={WAL_SIZE_LIMIT};")
        self._init_db()
        self._reader = sqlite3.connect(str(path), check_same_thread=False)
        self._reader.row_factory = sqlite3.Row

    def query(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        """Runs a read-only query on the read connection."""
        with self._read_lock:
            return self._reader.execute(sql, args).fetchall()

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
                CREATE TABLE IF NOT EXISTS results (""" + RESULTS_COLUMNS + """);
                CREATE TABLE IF NOT EXISTS result_content (
                    job_id       TEXT NOT NULL,
                    url          TEXT NOT NULL,
                    content      TEXT,
                    PRIMARY KEY (job_id, url)
                );
                CREATE INDEX IF NOT EXISTS jobs_created_idx ON jobs(created_at DESC);
                -- One row per search request (see `api/search.py`): no query, no
                -- answer, just enough to count them
                CREATE TABLE IF NOT EXISTS searches (
                    created_at   REAL NOT NULL,
                    provider     TEXT NOT NULL,
                    status       INTEGER NOT NULL,
                    results      INTEGER
                );
                CREATE INDEX IF NOT EXISTS searches_created_idx ON searches(created_at);
                """
            )
            self._add_outcome_columns()
            self._split_off_content()
            self._connection.executescript(RESULTS_INDEXES)
            self._connection.commit()
            self.version += 1

    def _split_off_content(self) -> None:
        """Moves the page content of a history from before `result_content` existed out
        of the results table: one-time, and on a history of gigabytes a matter of a
        minute or two. The results table is rebuilt without the column, and the file
        compacted, which needs as much free disk space as the file takes."""
        columns = [row["name"] for row in self._connection.execute("PRAGMA table_info(results)")]
        if "content" not in columns:
            return
        started = time.time()
        logger.warning("Moving stored page content out of the job history's results table. "
                       "One-time, and it may take a few minutes on a large history.")
        kept = ", ".join(c for c in columns if c != "content")
        self._connection.executescript(f"""
            BEGIN;
            INSERT OR REPLACE INTO result_content (job_id, url, content)
                SELECT job_id, url, content FROM results WHERE content IS NOT NULL;
            CREATE TABLE results_split ({RESULTS_COLUMNS});
            INSERT INTO results_split ({kept}) SELECT {kept} FROM results;
            DROP TABLE results;
            ALTER TABLE results_split RENAME TO results;
            COMMIT;
        """)
        moved = time.time() - started
        self._connection.execute("VACUUM")  # Gives the freed gigabytes back
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        logger.warning(f"Moved the page content in {moved:.0f} s, compacted the database "
                       f"in {time.time() - started - moved:.0f} s.")

    def _add_outcome_columns(self) -> None:
        """The outcome class of each result (see `scrapemm.common.outcome`), stored so
        it can be filtered and counted in SQL. Databases from before it existed get the
        columns added, and their rows classified from the errors they stored."""
        columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(results)")}
        if "outcome" not in columns:
            self._connection.execute("ALTER TABLE results ADD COLUMN outcome TEXT")
        if "outcome_kind" not in columns:
            self._connection.execute("ALTER TABLE results ADD COLUMN outcome_kind TEXT")
        if "queue_time" not in columns:  # Only recorded since; older rows have none
            self._connection.execute("ALTER TABLE results ADD COLUMN queue_time REAL")
        rows = self._connection.execute(
            "SELECT rowid, success, errors FROM results WHERE outcome IS NULL").fetchall()
        for row in rows:
            outcome, kind = classify(bool(row["success"]),
                                     json.loads(row["errors"]) if row["errors"] else {})
            self._connection.execute(
                "UPDATE results SET outcome = ?, outcome_kind = ? WHERE rowid = ?",
                (outcome, kind, row["rowid"]))
        if rows:
            logger.info(f"Classified the outcome of {len(rows)} stored results.")

    # --- Writing ------------------------------------------------------------------

    # For callers on the event loop: the same, in a worker thread
    async def astart(self, params: dict, url_count: int) -> str:
        from .workers import run_light
        return await run_light(self.start, params, url_count)

    async def arecord(self, job_id: str, payload: ResponsePayload, success: bool) -> None:
        from .workers import run_light
        await run_light(self.record, job_id, payload, success)

    async def afinish(self, job_id: str, succeeded: int, failed: int,
                      status: str = "completed") -> None:
        from .workers import run_light
        await run_light(self.finish, job_id, succeeded, failed, status)

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
        outcome, kind = classify(success, payload.errors)
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO results (job_id, url, success, method, errors, "
                "retrieval_time, queue_time, from_cache, created_at, outcome, outcome_kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, payload.url, int(success), payload.method,
                 json.dumps(payload.errors), payload.retrieval_time, payload.queue_time,
                 int(payload.from_cache), time.time(), outcome, kind))
            self._connection.execute(
                "INSERT OR REPLACE INTO result_content (job_id, url, content) VALUES (?, ?, ?)",
                (job_id, payload.url, content))
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
                 until: Optional[float], outcome: Optional[str] = None) -> tuple[str, list[Any]]:
        """Builds the WHERE clause the list and count queries share.

        The per-URL criteria (url, method, success, outcome) match a job if *any* of its
        results does, which is what somebody looking for "the job where example.com
        failed" actually means. `outcome` is a class (ok, unavailable, error) or a kind
        of unavailability (missing, captcha, paywall, ...).
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
        if outcome:
            if outcome in (OK, UNAVAILABLE, ERROR):
                result_clauses.append("results.outcome = ?")
            else:
                result_clauses.append("results.outcome_kind = ?")
            args.append(outcome)
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
                  until: Optional[float] = None, sort: str = "newest",
                  outcome: Optional[str] = None) -> list[dict]:
        where, args = self._filters(url, status, output_format, method, success,
                                    since, until, outcome)
        order = SORTS.get(sort, SORTS["newest"])
        query = f"SELECT * FROM jobs{where} ORDER BY {order}, created_at DESC LIMIT ? OFFSET ?"
        with self._read_lock:
            # `now` stands in for the end of jobs still running, when sorting by duration
            rows = self._reader.execute(
                query.replace(":now", str(time.time())), [*args, limit, offset]).fetchall()
            jobs = [_job_row(row) for row in rows]
            # Every listed job's URLs in one query, rather than one query per job
            ids = [job["id"] for job in jobs]
            by_job: dict[str, list[sqlite3.Row]] = {job_id: [] for job_id in ids}
            if ids:
                for row in self._reader.execute(
                        "SELECT job_id, url, success, method, retrieval_time, queue_time, from_cache, "
                        "errors, outcome, outcome_kind "
                        f"FROM results WHERE job_id IN ({','.join('?' * len(ids))}) "
                        "ORDER BY created_at", ids):
                    by_job[row["job_id"]].append(row)

        for job in jobs:
            _summarize(job, by_job[job["id"]])
        return jobs

    def count_jobs(self, status: Optional[str] = None, url: Optional[str] = None,
                   output_format: Optional[str] = None, method: Optional[str] = None,
                   success: Optional[bool] = None, since: Optional[float] = None,
                   until: Optional[float] = None, outcome: Optional[str] = None) -> int:
        where, args = self._filters(url, status, output_format, method, success,
                                    since, until, outcome)
        return self.query(f"SELECT COUNT(*) FROM jobs{where}", args)[0][0]

    def known_methods(self) -> list[str]:
        """Every method that has ever produced a result here, for the filter dropdown."""
        rows = self.query("SELECT DISTINCT method FROM results WHERE method IS NOT NULL "
                          "ORDER BY method")
        return [row["method"] for row in rows]

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._read_lock:
            row = self._reader.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            results = self._reader.execute(
                "SELECT results.*, result_content.content FROM results "
                "LEFT JOIN result_content USING (job_id, url) "
                "WHERE results.job_id = ? ORDER BY results.created_at", (job_id,)).fetchall()
        job = _job_row(row)
        job["results"] = [_result_row(r) for r in results]
        job["duration"] = _duration(job)
        # Live counts: the stored ones are only written once the job finishes, so a
        # running job would otherwise show none of what it has done so far
        done = {r["url"] for r in job["results"]}
        job["succeeded"] = sum(1 for r in job["results"] if r["success"])
        job["failed"] = len(job["results"]) - job["succeeded"]
        job["outcomes"] = _outcome_counts(r["outcome"] for r in job["results"])
        requested = list(dict.fromkeys(job["params"].get("urls") or [])) or list(done)
        job["pending"] = [url for url in requested if url not in done]
        return job

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._connection.execute("DELETE FROM results WHERE job_id = ?", (job_id,))
            self._connection.execute("DELETE FROM result_content WHERE job_id = ?", (job_id,))
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
            self._connection.execute(
                "DELETE FROM result_content WHERE job_id NOT IN (SELECT id FROM jobs)")
            self._connection.execute("DELETE FROM searches WHERE created_at < ?", (deadline,))
            self._connection.commit()
            self.version += 1
        if removed:
            logger.info(f"Pruned {removed} job records (media untouched).")
        return removed

    def stats(self) -> dict:
        """All-time figures. `succeeded` and `failed` are in scrapeMM's terms: a target
        that was unavailable counts as succeeded (see `scrapemm.common.outcome`);
        `outcomes` has the three classes apart."""
        jobs = self.query("SELECT COUNT(*) FROM jobs")[0][0]
        # One pass over the results for all figures
        results, retrieved, unavailable, cached = self.query(
            "SELECT COUNT(*), COALESCE(SUM(outcome = 'ok'), 0), "
            "COALESCE(SUM(outcome = 'unavailable'), 0), COALESCE(SUM(from_cache), 0) "
            "FROM results")[0]
        failed = results - retrieved - unavailable
        return {"jobs": jobs, "urls": results, "succeeded": retrieved + unavailable,
                "failed": failed, "from_cache": cached,
                "outcomes": {OK: retrieved, UNAVAILABLE: unavailable, ERROR: failed},
                "cache_hit_rate": cached / results if results else None}

    def method_counts(self) -> dict[str, int]:
        """How many URLs each method has successfully retrieved. Only successes count:
        a method that was tried and failed did not retrieve anything, and the number is
        meant to show what a method is actually carrying."""
        rows = self.query("SELECT method, COUNT(*) AS n FROM results "
                          "WHERE success = 1 AND method IS NOT NULL GROUP BY method")
        return {row["method"]: row["n"] for row in rows}

    def recent_success_rate(self, limit: int = 1000) -> dict:
        """The share of retrievals that succeeded, over the most recent `limit` of them.

        Recent rather than all-time on purpose: a server that has been running for
        months would otherwise average away exactly the degradation this number exists
        to surface.
        """
        rows = self.query("SELECT outcome FROM results ORDER BY created_at DESC LIMIT ?",
                          (limit,))
        total = len(rows)
        outcomes = _outcome_counts(row["outcome"] for row in rows)
        # scrapeMM's success: it did its part, whether or not the target had the content
        succeeded = outcomes[OK] + outcomes[UNAVAILABLE]
        return {
            "window": limit,
            "total": total,
            "succeeded": succeeded,
            "failed": total - succeeded,
            "outcomes": outcomes,
            # None rather than 100% when nothing has run: a fresh server has no rate,
            # and showing a perfect one would be a lie of omission.
            "rate": (succeeded / total) if total else None,
            # The share that actually yielded content
            "retrieved_rate": (outcomes[OK] / total) if total else None,
        }

    def recent_counts(self, seconds: float = RECENT_SPAN) -> list[list]:
        """The URLs retrieved in the last `seconds`, as [slot start, count] per slot of
        `SLOT` seconds that has any. The dashboard sums the slots since the viewer's
        own midnight: every time zone's offset is a multiple of 15 minutes, so the
        slots line up with every viewer's day, and one answer serves them all."""
        since = (time.time() - seconds) // SLOT * SLOT
        return [list(row) for row in self.query(
            f"SELECT CAST(created_at / {SLOT} AS INTEGER) * {SLOT} AS slot, COUNT(*) "
            f"FROM results WHERE created_at >= ? GROUP BY slot ORDER BY slot", (since,))]

    def record_search(self, provider: str, status: int, results: Optional[int]) -> None:
        """Counts one search request: which provider, its HTTP status, how many results."""
        with self._lock:
            self._connection.execute(
                "INSERT INTO searches (created_at, provider, status, results) VALUES (?, ?, ?, ?)",
                (time.time(), provider, status, results))
            self._connection.commit()
            self.version += 1

    def search_figures(self, seconds: float = RECENT_SPAN) -> dict:
        """The search requests of the last `seconds` per slot (as `recent_counts`), as
        [slot start, provider, succeeded, failed], and the all-time total."""
        since = (time.time() - seconds) // SLOT * SLOT
        rows = self.query(
            f"SELECT CAST(created_at / {SLOT} AS INTEGER) * {SLOT} AS slot, provider, "
            f"SUM(status = 200), SUM(status != 200) FROM searches WHERE created_at >= ? "
            f"GROUP BY slot, provider ORDER BY slot", (since,))
        return {"recent": [list(row) for row in rows],
                "total": self.query("SELECT COUNT(*) FROM searches")[0][0]}

    def close(self) -> None:
        with self._read_lock:
            self._reader.close()
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
                 "outcome": row["outcome"], "outcome_kind": row["outcome_kind"],
                 "method": row["method"], "retrieval_time": row["retrieval_time"],
                 "queue_time": row["queue_time"],
                 "from_cache": bool(row["from_cache"])}
        if not row["success"]:
            entry["error"] = _main_error(json.loads(row["errors"]) if row["errors"] else {})
        job["urls"].append(entry)

    # Live counts: the stored ones are only written once the job finishes
    job["done"] = len(done)
    job["succeeded"] = sum(1 for row in results if row["success"])
    job["failed"] = len(done) - job["succeeded"]
    job["outcomes"] = _outcome_counts(row["outcome"] for row in results)
    job["duration"] = _duration(job)
    job["methods"] = sorted({row["method"] for row in results if row["method"]})
    # The full list can be long and is on the detail page; only its size matters here
    job["params"] = {k: v for k, v in job["params"].items() if k != "urls"}


def _duration(job: dict) -> float:
    """How long the job took, start to finish -- or so far, while it runs. Not the sum
    of its URLs' retrieval times: those run concurrently and would add up to far more."""
    return (job["finished_at"] or time.time()) - job["created_at"]


def _main_error(errors: dict) -> Optional[dict]:
    """The error that decided a failed URL's outcome (see `decisive_error`), as
    {type, message, method}."""
    decisive = decisive_error(errors)
    if decisive is None:
        return None
    method, error = decisive
    return {"type": error.get("type"), "message": (error.get("message") or "")[:300],
            "method": method}


def _outcome_counts(outcomes) -> dict[str, int]:
    """How many of the given outcomes fall into each class (unknown ones are errors)."""
    counts = {OK: 0, UNAVAILABLE: 0, ERROR: 0}
    for outcome in outcomes:
        counts[outcome if outcome in counts else ERROR] += 1
    return counts


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
