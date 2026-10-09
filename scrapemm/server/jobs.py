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
import math
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

# How much job history is kept, unless the settings say otherwise (`job_retention_days`,
# `max_jobs`; 0 for no limit). Each job keeps its content, ~110 KB a URL, so 10,000 jobs
# of one URL already take about a gigabyte.
DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_JOBS = 10_000
PRUNE_INTERVAL = 3600  # Seconds between prunings while the server runs, see `app`


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
                    results      INTEGER,
                    api_key      TEXT
                );
                CREATE INDEX IF NOT EXISTS searches_created_idx ON searches(created_at);
                """
            )
            self._start_counters()
            self._add_outcome_columns()
            self._add_interruption_column()
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

    def _start_counters(self) -> None:
        """All-time totals, which pruning the history never lowers (see `stats()`). A
        database from before they existed starts them at what its history still holds:
        what was pruned before is gone."""
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'counters'").fetchone()
        if exists:
            return
        self._connection.execute(
            "CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        self._connection.execute(
            "INSERT INTO counters (name, value) VALUES "
            "('jobs', (SELECT COUNT(*) FROM jobs)), ('urls', (SELECT COUNT(*) FROM results)), "
            "('since', CAST(COALESCE((SELECT MIN(created_at) FROM jobs), strftime('%s', 'now')) AS INTEGER))")

    def _count(self, name: str, by: int = 1) -> None:
        """Adds to an all-time counter. Called with the write lock held."""
        self._connection.execute(
            "UPDATE counters SET value = value + ? WHERE name = ?", (by, name))

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
        searches = {row["name"] for row in self._connection.execute("PRAGMA table_info(searches)")}
        if "api_key" not in searches:  # Only recorded since; older rows have none
            self._connection.execute("ALTER TABLE searches ADD COLUMN api_key TEXT")
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

    def _add_interruption_column(self) -> None:
        """Who interrupted a job (see `scrapemm.server.interrupts`). Jobs interrupted
        before it was recorded have none."""
        columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(jobs)")}
        if "interrupted_by" not in columns:
            self._connection.execute("ALTER TABLE jobs ADD COLUMN interrupted_by TEXT")

    # --- Writing ------------------------------------------------------------------

    # For callers on the event loop: the same, in a worker thread
    async def astart(self, params: dict, url_count: int) -> str:
        from .workers import run_light
        return await run_light(self.start, params, url_count)

    async def arecord(self, job_id: str, payload: ResponsePayload, success: bool) -> None:
        from .workers import run_light
        await run_light(self.record, job_id, payload, success)

    async def afinish(self, job_id: str, succeeded: int, failed: int,
                      status: str = "completed", interrupted_by: Optional[str] = None) -> None:
        from .workers import run_light
        await run_light(self.finish, job_id, succeeded, failed, status, interrupted_by)

    def start(self, params: dict, url_count: int) -> str:
        """Opens a job. Every job runs under an API key, the one of whoever asked for it
        (`params["api_key"]`, its id): there is no run without one."""
        if not params.get("api_key"):
            raise ValueError("A job runs under the API key of whoever started it; none was given.")
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
            self._count("jobs")
            self._connection.commit()
            self.version += 1
        return job_id

    def record(self, job_id: str, payload: ResponsePayload, success: bool) -> None:
        content = None
        if payload.content is not None:
            stored = _truncate(payload.content.to_dict())
            if payload.screenshot is not None:
                # Kept with the content, which is what the job's page shows
                stored["screenshot"] = payload.screenshot.to_dict()
            content = json.dumps(stored)
        outcome, kind = classify(success, payload.errors)
        with self._lock:
            known = self._connection.execute(
                "SELECT 1 FROM results WHERE job_id = ? AND url = ?", (job_id, payload.url)).fetchone()
            if not known:  # A URL recorded again (a rerun) is still one URL
                self._count("urls")
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
               status: str = "completed", interrupted_by: Optional[str] = None) -> None:
        """Closes the job. An interrupted one says who interrupted it (user, client or
        server, see `scrapemm.server.interrupts`)."""
        with self._lock:
            self._connection.execute(
                "UPDATE jobs SET finished_at = ?, status = ?, succeeded = ?, failed = ?, "
                "interrupted_by = ? WHERE id = ?",
                (time.time(), status, succeeded, failed,
                 interrupted_by if status == "interrupted" else None, job_id))
            self._connection.commit()
            self.version += 1

    def interrupt_unfinished(self) -> int:
        """Marks the jobs a previous server process left running. Called at startup:
        nothing can still be working on them, and they would otherwise show as running
        forever."""
        with self._lock:
            count = self._connection.execute(
                "UPDATE jobs SET status = 'interrupted', finished_at = ?, interrupted_by = 'server' "
                "WHERE status = 'running'", (time.time(),)).rowcount
            self._connection.commit()
            self.version += 1
        return count

    def interrupt_stale(self, job_id: str, by: str) -> bool:
        """Closes a job that is running in the history but that nothing on this server
        works on (it was orphaned). False if it is not running."""
        with self._lock:
            changed = self._connection.execute(
                "UPDATE jobs SET status = 'interrupted', finished_at = ?, interrupted_by = ? "
                "WHERE id = ? AND status = 'running'", (time.time(), by, job_id)).rowcount
            self._connection.commit()
            self.version += 1
        return bool(changed)

    def job_state(self, job_id: str) -> Optional[dict]:
        """The status and the key of a job, without its results: None if there is none."""
        rows = self.query("SELECT status, params FROM jobs WHERE id = ?", (job_id,))
        if not rows:
            return None
        params = json.loads(rows[0]["params"]) if rows[0]["params"] else {}
        return {"status": rows[0]["status"], "api_key": params.get("api_key")}

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
            # Not the content itself, which runs to megabytes for a test run (see
            # `get_content()`), only the figures a result's header shows, computed by
            # SQLite where the content is
            results = self._reader.execute(
                f"SELECT results.*, {_CONTENT_STATS} FROM results "
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

    def get_content(self, job_id: str, url: str) -> Optional[dict]:
        """The stored content of one result of a job, None if there is none."""
        with self._read_lock:
            row = self._reader.execute(
                "SELECT content FROM result_content WHERE job_id = ? AND url = ?",
                (job_id, url)).fetchone()
        return json.loads(row["content"]) if row and row["content"] else None

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._connection.execute("DELETE FROM results WHERE job_id = ?", (job_id,))
            self._connection.execute("DELETE FROM result_content WHERE job_id = ?", (job_id,))
            self._connection.commit()
            self.version += 1
        return cursor.rowcount > 0

    # --- Housekeeping -------------------------------------------------------------

    def prune(self, retention_days: Optional[float] = None,
              max_jobs: Optional[int] = None) -> int:
        """Drops job records that are too old or too many: by the settings
        `job_retention_days` and `max_jobs` unless given, 0 meaning no limit. Never touches
        media, nor the all-time counters."""
        from .config import get_config_var
        if retention_days is None:
            retention_days = get_config_var("job_retention_days")
            retention_days = DEFAULT_RETENTION_DAYS if retention_days is None else retention_days
        if max_jobs is None:
            max_jobs = get_config_var("max_jobs")
            max_jobs = DEFAULT_MAX_JOBS if max_jobs is None else max_jobs
        deadline = time.time() - retention_days * 24 * 60 * 60 if retention_days > 0 else None
        with self._lock:
            removed = 0
            if deadline is not None:
                removed += self._connection.execute(
                    "DELETE FROM jobs WHERE created_at < ?", (deadline,)).rowcount
            if max_jobs > 0:
                removed += self._connection.execute(
                    "DELETE FROM jobs WHERE id NOT IN ("
                    "  SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)",
                    (int(max_jobs),)).rowcount
            self._connection.execute(
                "DELETE FROM results WHERE job_id NOT IN (SELECT id FROM jobs)")
            self._connection.execute(
                "DELETE FROM result_content WHERE job_id NOT IN (SELECT id FROM jobs)")
            if deadline is not None:
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
        counters = dict(self.query("SELECT name, value FROM counters"))
        return {"jobs": jobs, "urls": results, "succeeded": retrieved + unavailable,
                # Since counting began, whatever the history still holds
                "all_time": {"jobs": counters.get("jobs", jobs), "urls": counters.get("urls", results),
                             "since": counters.get("since")},
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

    def recent_scrape_times(self, limit: int = 1000) -> dict:
        """The median and 95th percentile of the retrieval time over the most recent
        `limit` actual scrapes (answers from the cache took no scraping and are left out)."""
        times = sorted(row[0] for row in self.query(
            "SELECT retrieval_time FROM results WHERE from_cache = 0 "
            "AND retrieval_time IS NOT NULL ORDER BY created_at DESC LIMIT ?", (limit,)))
        if not times:
            return {"window": limit, "total": 0, "median": None, "p95": None}
        return {"window": limit, "total": len(times),
                "median": times[len(times) // 2] if len(times) % 2
                else (times[len(times) // 2 - 1] + times[len(times) // 2]) / 2,
                "p95": times[min(len(times) - 1, math.ceil(0.95 * len(times)) - 1)]}

    def recent_counts(self, seconds: float = RECENT_SPAN) -> list[list]:
        """The URLs retrieved in the last `seconds`, as [slot start, count] per slot of
        `SLOT` seconds that has any. The dashboard sums the slots since the viewer's
        own midnight: every time zone's offset is a multiple of 15 minutes, so the
        slots line up with every viewer's day, and one answer serves them all."""
        since = (time.time() - seconds) // SLOT * SLOT
        return [list(row) for row in self.query(
            f"SELECT CAST(created_at / {SLOT} AS INTEGER) * {SLOT} AS slot, COUNT(*) "
            f"FROM results WHERE created_at >= ? GROUP BY slot ORDER BY slot", (since,))]

    def record_search(self, provider: str, status: int, results: Optional[int],
                      api_key: str) -> None:
        """Counts one search request: which provider, its HTTP status, how many results,
        and the API key (its id) it was made with."""
        if not api_key:
            raise ValueError("A search runs under the API key of whoever made it; none was given.")
        with self._lock:
            self._connection.execute(
                "INSERT INTO searches (created_at, provider, status, results, api_key) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), provider, status, results, api_key))
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


# What a result's header shows of its content, without the content: how much text, HTML
# and media came back. The text is the Markdown, or else the multimodal rendering.
_CONTENT_STATS = """
    result_content.content IS NOT NULL AS has_content,
    LENGTH(COALESCE(json_extract(result_content.content, '$.markdown'),
                    json_extract(result_content.content, '$.multimodal'), '')) AS stat_characters,
    LENGTH(CAST(COALESCE(json_extract(result_content.content, '$.html'), '') AS BLOB)) AS stat_html_bytes,
    (SELECT json_group_object(kind, n) FROM (
        SELECT json_extract(value, '$.kind') AS kind, COUNT(*) AS n
        FROM json_each(result_content.content, '$.items') GROUP BY kind)) AS stat_kinds,
    (SELECT COALESCE(SUM(json_extract(value, '$.size')), 0)
        FROM json_each(result_content.content, '$.items')) AS stat_bytes,
    (SELECT COUNT(*) FROM json_each(result_content.content, '$.items')
        WHERE json_extract(value, '$.size') IS NULL) AS stat_unsized
"""


def _result_row(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["errors"] = json.loads(result["errors"]) if result["errors"] else {}
    if "content" in result:
        result["content"] = json.loads(result["content"]) if result["content"] else None
    if "has_content" in result:
        # Only the figures; the content itself comes with `get_content()`
        kinds = json.loads(result.pop("stat_kinds") or "{}")
        result["has_content"] = bool(result["has_content"])
        result["stats"] = {
            "kinds": kinds, "media": sum(kinds.values()),
            "bytes": result.pop("stat_bytes") or 0, "unsized": result.pop("stat_unsized") or 0,
            "characters": result.pop("stat_characters") or 0,
            "html_bytes": result.pop("stat_html_bytes") or 0,
        } if result["has_content"] else None
        for key in ("stat_kinds", "stat_bytes", "stat_unsized", "stat_characters", "stat_html_bytes"):
            result.pop(key, None)
    result["success"] = bool(result["success"])
    result["from_cache"] = bool(result["from_cache"])
    return result


jobs = JobStore()
