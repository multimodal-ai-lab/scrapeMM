"""Measuring scrapeMM: a suite of known URLs, run on demand, scored for coverage and speed.

The suite starts out as the URLs of the retrieval tests (see scripts/build_test_suite.py),
each with the images and videos it must at least yield. Users add their own on top.

A run retrieves the whole suite as one job -- it shows up under Jobs like any other --
with the cache bypassed, since a cached answer measures the cache, not the scraping.
Each URL is scored against its expectation. Most entries expect content:

* passed  -- retrieved, with at least the expected media
* partial -- retrieved, but media is missing
* failed  -- not retrieved at all

An entry may instead expect the target to be unavailable (`"expect": "unavailable"`): a
private post, a removed page. It passes when scrapeMM classifies the result as
unavailable (see `scrapemm.common.outcome`) and fails otherwise -- when content came back
(the detection broke) or when the retrieval ran into an error of scrapeMM's own.

Only one run at a time: a second one would compete with the first for the same
concurrency slots and measure nothing but that.
"""

import asyncio
import json
import logging
import statistics
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import aiohttp

from scrapemm.common import ScrapingResponse
from scrapemm.common.outcome import UNAVAILABLE, classify
from scrapemm.common.paths import APP_NAME
from scrapemm.common.wire import errors_to_wire
from .paths import CONFIG_DIR

logger = logging.getLogger(APP_NAME)

DEFAULT_SUITE_PATH = Path(__file__).parent / "test_suite_default.json"
SUITE_PATH = CONFIG_DIR / "test_suite.json"  # The user's additions and removals
RUNS_PATH = CONFIG_DIR / "test_runs.json"
MAX_RUNS_KEPT = 20

OUTPUT_FORMAT = "multimodal"  # What the retrieval tests check
STRIP = True  # The suite measures the content without UI elements, see `strip_content()`

# What an entry may expect besides content (the default, when "expect" is absent)
EXPECTATIONS = (UNAVAILABLE,)


# --- The suite ------------------------------------------------------------------------

_suite_lock = threading.Lock()


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        logger.warning(f"Ignoring the unreadable {path}.")
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    partial.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    partial.replace(path)


def suite() -> list[dict]:
    """The URLs a run retrieves: the defaults the user kept, then the user's own."""
    changes = _read_json(SUITE_PATH, {"added": [], "removed": []})
    removed = set(changes.get("removed", []))
    entries = [dict(e, source="default") for e in _read_json(DEFAULT_SUITE_PATH, [])
               if e["url"] not in removed]
    known = {e["url"] for e in entries}
    for entry in changes.get("added", []):
        if entry["url"] not in known:
            entries.append(dict(entry, source="user"))
            known.add(entry["url"])
    return entries


def add(url: str, category: str = "Added", expected: Optional[dict] = None,
        expect: Optional[str] = None) -> dict:
    """Adds a URL, expecting at least `expected` media ({"image": n, "video": n}), or,
    with `expect="unavailable"`, expecting the target to be unavailable."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("A URL must start with http:// or https://.")
    if expect is not None and expect not in EXPECTATIONS:
        raise ValueError(f"An entry can expect content (the default) or one of: "
                         f"{', '.join(EXPECTATIONS)}; not '{expect}'.")
    expected = {k: int(v) for k, v in (expected or {}).items() if k in ("image", "video") and v}
    if expect and expected:
        raise ValueError("An entry that expects the target to be unavailable cannot "
                         "expect media as well.")
    entry = {"url": url, "expected": expected, "category": category.strip() or "Added"}
    if expect:
        entry["expect"] = expect
    with _suite_lock:
        changes = _read_json(SUITE_PATH, {"added": [], "removed": []})
        changes["removed"] = [u for u in changes.get("removed", []) if u != url]
        changes["added"] = [e for e in changes.get("added", []) if e["url"] != url]
        changes["added"].append(entry)
        _write_json(SUITE_PATH, changes)
    return entry


def remove(url: str) -> bool:
    """Takes a URL out of the suite: an added one is forgotten, a default one is
    remembered as removed (and comes back with `restore_defaults`)."""
    with _suite_lock:
        changes = _read_json(SUITE_PATH, {"added": [], "removed": []})
        before = len(changes.get("added", []))
        changes["added"] = [e for e in changes.get("added", []) if e["url"] != url]
        found = len(changes["added"]) < before
        if not found and any(e["url"] == url for e in _read_json(DEFAULT_SUITE_PATH, [])):
            changes.setdefault("removed", []).append(url)
            found = True
        _write_json(SUITE_PATH, changes)
    return found


def restore_defaults() -> int:
    with _suite_lock:
        changes = _read_json(SUITE_PATH, {"added": [], "removed": []})
        restored = len(changes.get("removed", []))
        changes["removed"] = []
        _write_json(SUITE_PATH, changes)
    return restored


# --- Scoring --------------------------------------------------------------------------

def _score(entry: dict, response) -> dict:
    """What happened to one URL, measured against its expectation."""
    sequence = response.content.multimodal if response.content else None
    found = {"image": len(sequence.images), "video": len(sequence.videos)} if sequence else {}
    expected = entry.get("expected") or {}
    expect = entry.get("expect")
    missing = {kind: count for kind, count in expected.items()
               if found.get(kind, 0) < count}
    result_class, result_kind = classify(response.success, response.errors)
    if expect:
        # The target must turn out unavailable: content, or an error of scrapeMM's own,
        # means the detection broke
        outcome = "passed" if result_class == expect else "failed"
    elif not response.success:
        outcome = "failed"
    elif missing:
        outcome = "partial"
    else:
        outcome = "passed"

    errors = errors_to_wire(response.errors) if not response.success else {}
    return {
        "url": entry["url"], "category": entry.get("category", "Other"),
        "outcome": outcome, "method": response.method,
        "retrieval_time": response.retrieval_time,  # From the first request, see `timing`
        "queue_time": response.queue_time,
        "expected": expected, "expect": expect, "found": found, "missing": missing,
        "result_class": result_class, "result_kind": result_kind,
        "error": _main_error(errors),
    }


def _main_error(errors: dict) -> Optional[dict]:
    from .jobs import _main_error as pick
    return pick(errors)


def summarize(results: list[dict], total: int, started: float,
              finished: Optional[float]) -> dict:
    """The statistics of a run so far -- also while it is still going."""
    done = len(results)
    counts = {o: sum(1 for r in results if r["outcome"] == o)
              for o in ("passed", "partial", "failed")}
    times = sorted(r["retrieval_time"] for r in results if r["retrieval_time"] is not None)
    elapsed = (finished or time.time()) - started

    def percentile(p: float) -> Optional[float]:
        if not times:
            return None
        return times[min(len(times) - 1, int(round(p * (len(times) - 1))))]

    categories: dict[str, dict] = {}
    for r in results:
        c = categories.setdefault(r["category"], {"category": r["category"], "done": 0,
                                                  "passed": 0, "partial": 0, "failed": 0,
                                                  "times": []})
        c["done"] += 1
        c[r["outcome"]] += 1
        if r["retrieval_time"] is not None:
            c["times"].append(r["retrieval_time"])
    for c in categories.values():
        c["median_time"] = statistics.median(c["times"]) if c["times"] else None
        del c["times"]

    methods: dict[str, int] = {}
    errors: dict[str, int] = {}
    for r in results:
        if r["outcome"] != "failed" and r["method"]:
            methods[r["method"]] = methods.get(r["method"], 0) + 1
        # An expected unavailability that was met is no error worth listing
        if r["outcome"] == "failed":
            kind = (r["error"] or {}).get("type") or "Unknown"
            errors[kind] = errors.get(kind, 0) + 1

    media = {kind: {"expected": sum(r["expected"].get(kind, 0) for r in results),
                    "found": sum(min(r["found"].get(kind, 0), r["expected"].get(kind, 0))
                                 for r in results)}
             for kind in ("image", "video")}

    return {
        "total": total, "done": done, **counts,
        # Coverage counts what yielded everything expected; retrieved includes partial
        "coverage": counts["passed"] / done if done else None,
        "retrieved": (counts["passed"] + counts["partial"]) / done if done else None,
        "elapsed": elapsed,
        "throughput": done / elapsed * 60 if elapsed > 0 else None,  # URLs per minute
        "time": {"median": percentile(0.5), "p90": percentile(0.9),
                 "max": times[-1] if times else None,
                 "mean": statistics.fmean(times) if times else None,
                 "all": times},
        "categories": sorted(categories.values(), key=lambda c: c["category"]),
        "methods": dict(sorted(methods.items(), key=lambda m: -m[1])),
        "errors": dict(sorted(errors.items(), key=lambda e: -e[1])),
        "media": media,
    }


# --- Running --------------------------------------------------------------------------

class TestRun:
    """The one run in flight, if any, and what it has produced so far."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self.id: Optional[str] = None
        self.job_id: Optional[str] = None
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.state = "idle"  # idle | running | completed | cancelled | failed
        self.entries: list[dict] = []
        self.results: list[dict] = []
        # The API key the run was started with, its id and name: the run is its job
        self.started_by: Optional[dict] = None
        # "completeness" or "speed": how the run retrieves (speed does not fall back
        # to archives, for one)
        self.prioritize = "completeness"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, started_by: dict, prioritize: str = "completeness") -> dict:
        """Starts a run of the suite under the API key `started_by` ({"id", "name"}), retrieving
        with the given priority (completeness or speed)."""
        if self.running:
            return self.status()
        if prioritize not in ("completeness", "speed"):
            raise ValueError("The priority is either completeness or speed.")
        if not (started_by or {}).get("id"):
            raise ValueError("A test run runs under the API key of whoever starts it.")
        entries = suite()
        if not entries:
            raise ValueError("The suite is empty; add URLs first.")
        self.id = uuid.uuid4().hex[:10]
        self.started_by = started_by
        self.prioritize = prioritize
        self.entries, self.results = entries, []
        self.started, self.finished, self.state = time.time(), None, "running"
        self._task = asyncio.create_task(self._run())
        return self.status()

    async def cancel(self) -> dict:
        if self.running:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        return self.status()

    async def _run(self) -> None:
        from .api.retrieve import _to_payload
        from .engine import retrieve_one
        from .jobs import jobs

        urls = [e["url"] for e in self.entries]
        self.job_id = await jobs.astart({"urls": urls, "output_format": OUTPUT_FORMAT,
                                         "strip": STRIP, "use_cache": False, "test_run": self.id,
                                         "prioritize": self.prioritize,
                                         "api_key": self.started_by["id"]}, len(urls))
        passed = failed = 0
        tasks: list[asyncio.Task] = []
        try:
            async with aiohttp.ClientSession() as session:
                async def one(entry: dict):
                    try:
                        return entry, await retrieve_one(entry["url"], session,
                                                         output_format=OUTPUT_FORMAT,
                                                         use_cache=False, strip=STRIP,
                                                         prioritize=self.prioritize)
                    except Exception as e:
                        # One URL must not end the run; it counts as failed, with why
                        logger.warning(f"Test retrieval of {entry['url']} raised.", exc_info=True)
                        return entry, ScrapingResponse(url=entry["url"], content=None,
                                                       errors={"scrapemm": e},
                                                       output_format=OUTPUT_FORMAT)

                tasks = [asyncio.create_task(one(e)) for e in self.entries]
                for completed in asyncio.as_completed(tasks):
                    entry, response = await completed
                    await jobs.arecord(self.job_id, _to_payload(response), response.success)
                    self.results.append(_score(entry, response))
                    passed += response.success
                    failed += not response.success
            self.state = "completed"
            await jobs.afinish(self.job_id, passed, failed)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            self.state = "cancelled"
            jobs.finish(self.job_id, passed, failed, status="interrupted")
            raise
        except Exception:
            logger.error("The test run failed.", exc_info=True)
            self.state = "failed"
            jobs.finish(self.job_id, passed, failed, status="failed")
        finally:
            self.finished = time.time()
            _save_run(self.report())

    def rerun_captchas(self, started_by: dict) -> dict:
        """Retrieves again the URLs of the latest run that ran into a CAPTCHA, now that
        somebody may have solved it, and puts their new results into that same report.
        The cache is allowed here: solving drains the queued URLs into it (Archive.today
        into its permanent page cache), which is exactly what this is meant to pick up."""
        if self.running:
            raise ValueError("A test run is under way.")
        if not (started_by or {}).get("id"):
            raise ValueError("A rerun runs under the API key of whoever starts it.")
        base = self.report() if self.id else _latest_report()
        if base is None:
            raise ValueError("There is no test run to rerun URLs of.")
        gated = [r for r in base["results"] if _is_captcha(r)]
        if not gated:
            raise ValueError("No URL of the latest run ran into a CAPTCHA.")

        # Continue the latest report: its results stand, except for the gated ones
        self.id, self.job_id = base["id"], base.get("job_id")
        self.started, self.finished = base.get("started"), base.get("finished")
        self.results = [r for r in base["results"] if not _is_captcha(r)]
        self.entries = [{"url": r["url"], "category": r["category"], "expected": r["expected"]}
                        for r in base["results"]]
        final_state = base.get("state", "completed")
        self.started_by = base.get("started_by")  # The run's; the rerun's own key is its job's
        self.prioritize = base.get("prioritize") or "completeness"  # As the run retrieved
        self.state = "running"
        self._task = asyncio.create_task(self._rerun(gated, final_state, started_by))
        return self.status()

    async def _rerun(self, gated: list[dict], final_state: str, started_by: dict) -> None:
        from .api.retrieve import _to_payload
        from .engine import retrieve_one
        from .jobs import jobs

        urls = [r["url"] for r in gated]
        job_id = await jobs.astart({"urls": urls, "output_format": OUTPUT_FORMAT, "strip": STRIP,
                             "use_cache": True, "prioritize": self.prioritize,
                             "test_run": self.id, "rerun": "captcha",
                             "api_key": started_by["id"]}, len(urls))
        passed = failed = 0
        try:
            async with aiohttp.ClientSession() as session:
                async def one(entry: dict):
                    try:
                        return entry, await retrieve_one(entry["url"], session,
                                                         output_format=OUTPUT_FORMAT,
                                                         use_cache=True, strip=STRIP,
                                                         prioritize=self.prioritize)
                    except Exception as e:
                        logger.warning(f"Test rerun of {entry['url']} raised.", exc_info=True)
                        return entry, ScrapingResponse(url=entry["url"], content=None,
                                                       errors={"scrapemm": e},
                                                       output_format=OUTPUT_FORMAT)

                for completed in asyncio.as_completed([one(r) for r in gated]):
                    entry, response = await completed
                    await jobs.arecord(job_id, _to_payload(response), response.success)
                    # Its result lives in the rerun's job, not the run's
                    self.results.append(_score(entry, response) | {"job_id": job_id})
                    passed += response.success
                    failed += not response.success
            await jobs.afinish(job_id, passed, failed)
        except asyncio.CancelledError:
            # The URLs not rerun keep their earlier CAPTCHA result
            done = {r["url"] for r in self.results}
            self.results += [r for r in gated if r["url"] not in done]
            jobs.finish(job_id, passed, failed, status="interrupted")
            raise
        finally:
            # The run's own figures stand: a rerun corrects results, it is no new run
            self.state = final_state
            _save_run(self.report())

    def report(self) -> dict:
        return {
            "id": self.id, "job_id": self.job_id, "state": self.state,
            "started": self.started, "finished": self.finished, "started_by": self.started_by,
            "prioritize": self.prioritize,
            "summary": summarize(self.results, len(self.entries), self.started or time.time(),
                                 self.finished),
            "results": sorted(self.results, key=lambda r: (r["category"], r["url"])),
        }

    def status(self) -> dict:
        if self.id is None:
            return {"state": "idle"}
        report = self.report()
        report["running"] = self.running
        # What is still under way, so the UI can show it as such
        done = {r["url"] for r in self.results}
        report["pending"] = [{"url": e["url"], "category": e.get("category", ""),
                              "expected": e.get("expected") or {}}
                             for e in self.entries if e["url"] not in done]
        return report


def _save_run(report: dict) -> None:
    runs = [r for r in _read_json(RUNS_PATH, []) if r.get("id") != report["id"]]
    runs.append(report)
    try:
        _write_json(RUNS_PATH, runs[-MAX_RUNS_KEPT:])
    except OSError:
        logger.warning(f"Could not save the test report to {RUNS_PATH}.", exc_info=True)


def history() -> list[dict]:
    """The kept runs, newest first, without their per-URL results."""
    return [{k: v for k, v in r.items() if k != "results"}
            | {"summary": {k: v for k, v in r["summary"].items()
                           if k not in ("categories", "time")}
               | {"time": {k: v for k, v in r["summary"]["time"].items() if k != "all"}}}
            for r in reversed(_read_json(RUNS_PATH, []))]


def past_run(run_id: str) -> Optional[dict]:
    return next((r for r in _read_json(RUNS_PATH, []) if r.get("id") == run_id), None)


def _latest_report() -> Optional[dict]:
    runs = _read_json(RUNS_PATH, [])
    return runs[-1] if runs else None


def _is_captcha(result: dict) -> bool:
    return (result.get("outcome") == "failed"
            and (result.get("error") or {}).get("type") == "CaptchaEncounteredError")


run = TestRun()
