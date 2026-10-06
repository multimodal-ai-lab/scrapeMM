"""Statistics of past retrievals over time, for the Statistics view.

Everything is aggregated from the job history's `results` table (one row per retrieved
URL). The counts per time bucket and group are one GROUP BY in SQL, over the rows of the
requested range only (`results_created_idx`), so the cost follows the range, not the size
of the history. Retrieval times and failing domains need the individual rows of the
range; those are read as a few narrow columns and reduced in Python.

Buckets are aligned to the viewer's local time (`tz_offset`, minutes east of UTC, as the
browser reports it negated): a "day" runs from local midnight to midnight, a "week" from
Monday to Monday.
"""

import statistics
import time
from collections import Counter, defaultdict
from typing import Literal, Optional
from urllib.parse import urlsplit

from scrapemm.common.outcome import ERROR, OK, UNAVAILABLE

Bucket = Literal["hour", "day", "week"]
Group = Literal["method", "outcome", "kind", "key"]

BUCKET_SECONDS = {"hour": 3600, "day": 86400, "week": 7 * 86400}
# How many buckets a view spans unless asked otherwise: two days, a month, half a year
DEFAULT_PERIODS = {"hour": 48, "day": 30, "week": 26}
MAX_PERIODS = 400
# 1970-01-05, the first Monday after the epoch: weeks start on Mondays
WEEK_ANCHOR = 4 * 86400
# Methods shown on their own; the rest fold into "Other"
MAX_METHOD_SERIES = 7
NO_METHOD = "(none)"  # Failed retrievals: no method delivered anything
NO_KEY = "(none)"  # Jobs without a key: test runs, and retrievals before keys were recorded
OTHER = "Other"
TOP_DOMAINS = 8


def _bucket_start(t: float, size: int, offset: int, anchor: int) -> int:
    """The start (UTC seconds) of the bucket that local time `t + offset` falls into."""
    return int((t + offset - anchor) // size * size + anchor - offset)


def _percentile(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p * (len(values) - 1))))]


def _domain(url: str) -> str:
    try:
        host = urlsplit(url).hostname or url
    except ValueError:
        return url
    return host.removeprefix("www.")


def _key_names() -> dict[str, str]:
    """The current name of every key, by id: a renamed key's retrievals show under its
    new name, a revoked key's are kept but no longer named."""
    from .auth import ROOT_PRINCIPAL, key_store
    return {ROOT_PRINCIPAL.id: ROOT_PRINCIPAL.name} | {k["id"]: k["name"] for k in key_store().list()}


def retrieval_stats(store, bucket: Bucket = "day", group: Group = "outcome",
                    periods: Optional[int] = None, tz_offset: int = 0,
                    now: Optional[float] = None) -> dict:
    """The retrievals of the last `periods` buckets (including the current one), counted
    per bucket and per group (method, outcome class, kind of outcome, or API key), plus what the
    secondary charts need: outcome and cache counts per bucket, retrieval times per
    bucket and per method, the domains that failed most and each method's share."""
    size = BUCKET_SECONDS[bucket]
    periods = max(1, min(int(periods or DEFAULT_PERIODS[bucket]), MAX_PERIODS))
    offset = int(tz_offset) * 60
    anchor = WEEK_ANCHOR if bucket == "week" else 0
    now = time.time() if now is None else now
    current = _bucket_start(now, size, offset, anchor)
    starts = [current - size * i for i in range(periods - 1, -1, -1)]
    since = starts[0]
    index = {start: i for i, start in enumerate(starts)}

    # Bucket start, computed in SQL: the same arithmetic as _bucket_start()
    bucket_sql = (f"(CAST((created_at + {offset} - {anchor}) / {size} AS INTEGER) * {size}"
                  f" + {anchor} - {offset})")
    source = "results"
    if group == "method":
        key_sql = f"COALESCE(method, '{NO_METHOD}')"
    elif group == "key":
        # A retrieval's key is its job's, recorded among the job's parameters
        source = ("(SELECT results.created_at AS created_at, "
                  "json_extract(jobs.params, '$.api_key') AS api_key "
                  "FROM results LEFT JOIN jobs ON jobs.id = results.job_id)")
        key_sql = f"COALESCE(api_key, '{NO_KEY}')"
    elif group == "kind":
        key_sql = "CASE WHEN outcome = 'unavailable' THEN 'unavailable:' || " \
                  "COALESCE(outcome_kind, 'missing') ELSE COALESCE(outcome, 'error') END"
    else:
        key_sql = "COALESCE(outcome, 'error')"

    # On the read connection: the job history's writes go on meanwhile
    with store._read_lock:
        grouped = store._reader.execute(
            f"SELECT {bucket_sql} AS b, {key_sql} AS k, COUNT(*) AS n FROM {source} "
            f"WHERE created_at >= ? GROUP BY b, k", (since,)).fetchall()
        overall = store._reader.execute(
            f"SELECT {bucket_sql} AS b, COUNT(*) AS n, "
            f"COALESCE(SUM(outcome = 'ok'), 0) AS ok, "
            f"COALESCE(SUM(outcome = 'unavailable'), 0) AS unavailable, "
            f"COALESCE(SUM(from_cache), 0) AS cached "
            f"FROM results WHERE created_at >= ? GROUP BY b", (since,)).fetchall()
        # Times of real retrievals only: a cache hit measures the cache. Retrieval
        # times run from the first request on; the wait before it is `queue_time`,
        # recorded since October 2026 (see `timing`)
        timed = store._reader.execute(
            f"SELECT {bucket_sql} AS b, method, retrieval_time, queue_time FROM results "
            f"WHERE created_at >= ? AND outcome = 'ok' AND from_cache = 0 "
            f"AND retrieval_time IS NOT NULL", (since,)).fetchall()
        failing = store._reader.execute(
            "SELECT url FROM results WHERE created_at >= ? AND outcome = 'error'",
            (since,)).fetchall()

    # --- The main chart: counts per bucket and group --------------------------------
    per_key: dict[str, list[int]] = defaultdict(lambda: [0] * periods)
    for row in grouped:
        i = index.get(row["b"])
        if i is not None:
            per_key[row["k"]][i] += row["n"]
    series = [{"key": k, "counts": c, "total": sum(c)} for k, c in per_key.items()]
    if group in ("method", "key"):
        # The most used methods (keys) on their own, the rest together; none last
        if group == "key":
            names = _key_names()
            for s in series:
                s["name"] = names.get(s["key"], "Revoked key") if s["key"] != NO_KEY else None
        methods = sorted((s for s in series if s["key"] != NO_METHOD), key=lambda s: -s["total"])
        shown, rest = methods[:MAX_METHOD_SERIES], methods[MAX_METHOD_SERIES:]
        if rest:
            shown.append({"key": OTHER, "total": sum(s["total"] for s in rest),
                          "counts": [sum(c) for c in zip(*(s["counts"] for s in rest))],
                          "members": [s.get("name") or s["key"] for s in rest]})
        series = shown + [s for s in series if s["key"] == NO_METHOD]
    else:
        order = [OK, UNAVAILABLE, ERROR] if group == "outcome" else \
            [OK] + [f"unavailable:{k}" for k in
                    ("missing", "paywall", "captcha", "blocked", "rate_limit", "unsupported")] + [ERROR]
        series.sort(key=lambda s: order.index(s["key"]) if s["key"] in order else len(order))

    # --- Per bucket: outcomes, cache, times ------------------------------------------
    total = [0] * periods
    ok = [0] * periods
    unavailable = [0] * periods
    cached = [0] * periods
    for row in overall:
        i = index.get(row["b"])
        if i is None:
            continue
        total[i] += row["n"]
        ok[i] += row["ok"]
        unavailable[i] += row["unavailable"]
        cached[i] += row["cached"]

    times: list[list[float]] = [[] for _ in range(periods)]
    waits: list[list[float]] = [[] for _ in range(periods)]
    by_method: dict[str, list[float]] = defaultdict(list)
    for row in timed:
        i = index.get(row["b"])
        if i is not None:
            times[i].append(row["retrieval_time"])
            if row["queue_time"] is not None:
                waits[i].append(row["queue_time"])
        by_method[row["method"] or NO_METHOD].append(row["retrieval_time"])

    domains = Counter(_domain(row["url"]) for row in failing)
    grand = sum(total)
    return {
        "bucket": bucket, "group": group, "periods": periods, "size": size,
        "since": since, "until": current + size, "tz_offset": tz_offset,
        "buckets": starts,
        "series": series,
        "per_bucket": {
            "total": total, OK: ok, UNAVAILABLE: unavailable,
            ERROR: [t - o - u for t, o, u in zip(total, ok, unavailable)],
            "cached": cached,
            "median_time": [statistics.median(v) if v else None for v in times],
            "p90_time": [_percentile(v, 0.9) for v in times],
            "median_queue_time": [statistics.median(v) if v else None for v in waits],
        },
        "summary": {
            "total": grand, OK: sum(ok), UNAVAILABLE: sum(unavailable),
            ERROR: grand - sum(ok) - sum(unavailable), "cached": sum(cached),
            "median_time": statistics.median(t for v in times for t in v)
            if any(times) else None,
            "median_queue_time": statistics.median(t for v in waits for t in v)
            if any(waits) else None,
        },
        "methods": sorted(
            ({"method": m, "count": len(v), "median_time": statistics.median(v),
              "p90_time": _percentile(v, 0.9)} for m, v in by_method.items()),
            key=lambda m: -m["count"]),
        "failing_domains": [{"domain": d, "count": n} for d, n in domains.most_common(TOP_DOMAINS)],
    }
