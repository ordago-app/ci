"""Read-only statistics over the controller's ``events`` log.

Pure analysis: no writes, no network, no controller imports. Every number this
module produces feeds config decisions, so correctness beats elegance —
in particular, every count below is COUNT(DISTINCT job_id), never a row
count. A queued job re-defers on every poll tick, so counting rows instead of
distinct jobs overstates demand by roughly 17x on this dataset.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime

# already_running means "this job already has a lane" — it is not a capacity
# gate and must never appear in gate statistics.
EXCLUDED_REASONS = {"already_running"}

# not_allowlisted means "this repo/job is not served here at all" — an eligibility
# check, not a capacity gate. No amount of RAM/lane/disk tuning changes it, so mixing
# it into the capacity-gate table would let a reader mistake it for something tuning
# could fix. It stays in gate_counts()/binding_gate_counts() (the count is genuinely
# useful — it says jobs are queueing that this controller will never serve) but the
# rendering layer below reports it in its own table, separate from capacity gates.
ELIGIBILITY_REASONS = {"not_allowlisted"}

# 2026-07-30 00:00 UTC. peak_ram_mb recorded before this counted reclaimable
# page cache and is unusable — see the module docstring in the capacity plan.
# A previous tuning round quoted a pre-fix percentile and drew a wrong
# conclusion from it; render_report refuses to repeat that by defaulting
# every peak-RAM query to this cutoff and printing the cutoff in the output.
PAGE_CACHE_FIX_TS = 1785369600


def _rows(conn: sqlite3.Connection, sql: str, params: Sequence[float | str]) -> sqlite3.Cursor:
    """Run a query with dict-style row access, without touching conn.row_factory.

    Setting row_factory on the connection would silently change the row type
    for every other query a caller runs on it later. A cursor's row_factory
    is local to that cursor, so callers who hand us their own MetricsStore
    connection (whose row_factory is the tuple default) get it back
    unmodified.
    """
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    cur.execute(sql, params)
    return cur


def available_columns(conn: sqlite3.Connection) -> set[str]:
    """Columns actually present on the events table of THIS database.

    ci_bench opens the DB read-only (open_readonly), so unlike MetricsStore it
    can never run the additive migration in metrics.py — a snapshot pulled
    from a controller that hasn't been redeployed since a column was added
    (reasons, job_name, workflow, host, conclusion) simply won't have it.
    Every stat that reads one of those columns must check here first and
    degrade instead of letting sqlite3.OperationalError ("no such column")
    propagate.
    """
    return {row["name"] for row in _rows(conn, "PRAGMA table_info(events)", [])}


def open_readonly(db_path: str) -> sqlite3.Connection:
    """Open the controller's DB read-only via URI, with a busy timeout.

    The controller writes this file with a rollback journal. A plain
    sqlite3.connect on a wrong path silently creates an empty DB instead of
    erroring, which would make this tool confidently report zeros.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


def percentile(values: list[int], p: float) -> int | None:
    """Interpolated percentile. None for an empty list, never a crash."""
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p / 100
    floor = int(k)
    ceil = min(floor + 1, len(ordered) - 1)
    return round(ordered[floor] + (ordered[ceil] - ordered[floor]) * (k - floor))


def gate_counts(conn: sqlite3.Connection, since: float | None = None) -> dict[str, int]:
    """Distinct jobs per PRIMARY reason (the `reason` column).

    This undercounts secondary gates: a job deferred for
    "lane_ceiling,budget_full" is attributed only to lane_ceiling here.
    Use binding_gate_counts() for the masking-corrected view.
    """
    seen: dict[str, set[int]] = {}
    sql = "SELECT job_id, reason FROM events WHERE kind='defer'"
    params: list[float] = []
    if since is not None:
        sql += " AND ts > ?"
        params.append(since)
    for row in _rows(conn, sql, params):
        gate = row["reason"]
        if gate and gate not in EXCLUDED_REASONS:
            seen.setdefault(gate, set()).add(row["job_id"])
    return {gate: len(jobs) for gate, jobs in sorted(seen.items(), key=lambda kv: -len(kv[1]))}


def binding_gate_counts(
    conn: sqlite3.Connection,
    since: float | None = None,
    config_version: str | None = None,
) -> dict[str, int]:
    """Distinct jobs blocked by each gate, counting EVERY binding gate.

    gate_counts() attributes a job only to its first gate, which is how
    lane_ceiling came to mask budget_full. Rows written before the multi-gate
    change have reasons IS NULL; fall back to reason so history stays usable.

    If this database predates the `reasons` column entirely (a snapshot from
    a controller that hasn't been redeployed with the multi-gate change
    yet), every row falls back to `reason` — this returns the same counts as
    gate_counts() in that case, which is the correct (if less complete)
    answer, not an error. Callers that need to know whether the corrected
    view is actually available should check available_columns() themselves;
    render_report does, so the report can say so honestly instead of
    implying the masking bias is zero.

    `since` and `config_version` are independently combinable filters —
    passing both applies both (AND), not either-or.
    """
    has_reasons = "reasons" in available_columns(conn)
    seen: dict[str, set[int]] = {}
    select = "job_id, reason, reasons" if has_reasons else "job_id, reason"
    sql = f"SELECT {select} FROM events WHERE kind='defer'"
    params: list[float | str] = []
    if since is not None:
        sql += " AND ts > ?"
        params.append(since)
    if config_version is not None:
        sql += " AND config_version = ?"
        params.append(config_version)
    for row in _rows(conn, sql, params):
        reasons_val = row["reasons"] if has_reasons else None
        gates = (reasons_val or row["reason"] or "").split(",")
        for gate in gates:
            if gate and gate not in EXCLUDED_REASONS:
                seen.setdefault(gate, set()).add(row["job_id"])
    return {gate: len(jobs) for gate, jobs in sorted(seen.items(), key=lambda kv: -len(kv[1]))}


def class_peaks(
    conn: sqlite3.Connection,
    since: float | None = None,
    config_version: str | None = None,
) -> dict[str, dict[str, int]]:
    """Per-class peak-RAM distribution (n/p50/p90/p95/max) from reap events.

    One reap row exists per completed job, so no distinct-job dedup is
    needed here (unlike defer events, reaps don't repeat per poll tick).

    `since` and `config_version` are independently combinable filters —
    passing both applies both (AND), not either-or.
    """
    per_class: dict[str, list[int]] = {}
    sql = "SELECT class, peak_ram_mb FROM events WHERE kind='reap' AND peak_ram_mb IS NOT NULL"
    params: list[float | str] = []
    if since is not None:
        sql += " AND ts > ?"
        params.append(since)
    if config_version is not None:
        sql += " AND config_version = ?"
        params.append(config_version)
    for row in _rows(conn, sql, params):
        cls = row["class"]
        if cls:
            per_class.setdefault(cls, []).append(row["peak_ram_mb"])

    result: dict[str, dict[str, int]] = {}
    for cls, peaks in per_class.items():
        result[cls] = {
            "n": len(peaks),
            "p50": percentile(peaks, 50) or 0,
            "p90": percentile(peaks, 90) or 0,
            "p95": percentile(peaks, 95) or 0,
            "max": max(peaks),
        }
    return result


def queue_waits(
    conn: sqlite3.Connection,
    since: float | None = None,
    config_version: str | None = None,
) -> dict[str, list[float]]:
    """Per-class queue wait: each job's first defer ts to its admit ts.

    Keyed by the class recorded on the ADMIT row (a job's class is stable,
    but the admit row is the one guaranteed to carry it). Non-positive
    waits (bad clocks, out-of-order writes) are dropped rather than
    reported as negative latency.

    A job's first-defer lookup is deliberately UNFILTERED by `since` or
    `config_version`: a queued job re-defers on every poll tick, so a job
    first deferred before the window (or under an earlier config_version)
    that keeps re-deferring inside it would otherwise have its true first
    defer masked by the earliest IN-WINDOW re-defer, understating its wait
    as `admit_ts - window_start` — a systematic downward bias that hits the
    longest waits hardest (the p90/p95 this metric exists to measure). Only
    the admit side is filtered, since that determines which jobs are in the
    reported window at all.

    `since` and `config_version` are independently combinable filters on
    the admit side — passing both applies both (AND), not either-or.
    """
    first_defers = {
        row["job_id"]: row["first_defer_ts"]
        for row in _rows(
            conn,
            "SELECT job_id, MIN(ts) AS first_defer_ts FROM events "
            "WHERE kind='defer' GROUP BY job_id",
            [],
        )
    }

    admit_sql = "SELECT job_id, ts, class FROM events WHERE kind='admit'"
    admit_params: list[float | str] = []
    if since is not None:
        admit_sql += " AND ts > ?"
        admit_params.append(since)
    if config_version is not None:
        admit_sql += " AND config_version = ?"
        admit_params.append(config_version)

    waits: dict[str, list[float]] = {}
    for row in _rows(conn, admit_sql, admit_params):
        defer_ts = first_defers.get(row["job_id"])
        if defer_ts is None:
            continue
        wait = row["ts"] - defer_ts
        if wait <= 0:
            continue
        cls = row["class"] or "unknown"
        waits.setdefault(cls, []).append(wait)
    return waits


def _reasons_validity_floor(conn: sqlite3.Connection) -> float | None:
    """Earliest ts at which this database started recording multi-gate `reasons`.

    The `reasons` column existing is NOT the same as its data being valid.
    `binding_gate_counts` deliberately falls back to the single `reason` column for
    rows written before the multi-gate controller (that fallback is what keeps the
    112k historical rows visible at all). But those rows carry the MASKED view — one
    gate per job — so counting them as "binding-gate jobs" would blend corrected and
    masked data under the corrected label, reintroducing exactly the bias this report
    exists to expose, for any window spanning the migration.

    Like the conclusion floor, there is no fixed calendar date (it depends on when the
    operator deploys), so it is derived: the first defer row that ever recorded a
    non-NULL `reasons`. Returns None when the column is absent, or present but never
    yet written — in both cases the binding view is unavailable, not zero.
    """
    if "reasons" not in available_columns(conn):
        return None
    row = next(
        _rows(
            conn,
            "SELECT MIN(ts) AS floor_ts FROM events WHERE kind='defer' AND reasons IS NOT NULL",
            [],
        ),
        None,
    )
    return None if row is None or row["floor_ts"] is None else float(row["floor_ts"])


def _conclusion_validity_floor(conn: sqlite3.Connection) -> float | None:
    """Earliest ts at which this database started recording a `conclusion`.

    Rows written before the controller that writes `conclusion` was deployed
    have conclusion IS NULL because the column did not exist yet — not
    because anything failed. Unlike PAGE_CACHE_FIX_TS there is no fixed
    calendar date for this cutoff (it depends on when the operator deploys),
    so it is derived from the data: the first reap row that ever recorded a
    non-NULL conclusion marks the point the column started being written.

    Returns None if the `conclusion` column is absent entirely, OR if it
    exists but no reap row has ever recorded a non-NULL value yet (deployed,
    but no post-deploy reap has landed). Both cases mean "we cannot yet tell
    genuine infra failures from column-didn't-exist-yet NULLs", so callers
    must report "unknown" rather than a number.
    """
    if "conclusion" not in available_columns(conn):
        return None
    row = _rows(
        conn,
        "SELECT MIN(ts) AS floor_ts FROM events WHERE kind='reap' AND conclusion IS NOT NULL",
        [],
    ).fetchone()
    return row["floor_ts"]


def infra_failures(
    conn: sqlite3.Connection,
    since: float | None = None,
    config_version: str | None = None,
) -> int | None:
    """Reap events that never reached a terminal GitHub conclusion.

    Two shapes count: a NULL conclusion (the controller looked up the job and
    GitHub reported nothing terminal) and the explicit `'infra_failure'` sentinel
    (Task 13 — the controller reaped the lane without even attempting a lookup,
    because the lane's host had already disappeared mid-job). Counting only NULL
    would make Task 13's whole reason for existing invisible in this report.

    Counted only at or after `_conclusion_validity_floor()`: rows older than
    that are NULL because the `conclusion` column did not exist yet, not
    because anything failed, so they must never be counted as infra
    failures. See the module-level note in the plan: on first deploy this
    would otherwise report ~4,000 false infra failures from history alone.

    "Lookup failed" (exception swallowed) and "adopted" (lookup skipped, no
    repo to query) are written as distinct sentinel strings by the
    controller — see lookup_failures() — so this predicate must not match them:
    only NULL and the 'infra_failure' sentinel mean "genuinely no terminal
    conclusion", never "we don't know" or "this lane was adopted, not reaped".

    Returns None if the validity floor cannot be derived (see
    _conclusion_validity_floor) — reporting 0 would misrepresent "cannot
    tell yet" as "no failures observed", which is a real, wrong finding.

    `since` and `config_version` are independently combinable filters —
    passing both applies both (AND), not either-or.
    """
    floor = _conclusion_validity_floor(conn)
    if floor is None:
        return None
    sql = (
        "SELECT COUNT(DISTINCT job_id) AS n FROM events "
        "WHERE kind='reap' AND (conclusion IS NULL OR conclusion = 'infra_failure') "
        "AND ts >= ?"
    )
    params: list[float | str] = [floor]
    if since is not None:
        sql += " AND ts > ?"
        params.append(since)
    if config_version is not None:
        sql += " AND config_version = ?"
        params.append(config_version)
    row = _rows(conn, sql, params).fetchone()
    return int(row["n"])


def lookup_failures(
    conn: sqlite3.Connection,
    since: float | None = None,
    config_version: str | None = None,
) -> int | None:
    """Reap events where the GitHub conclusion lookup itself failed (exception swallowed).

    Distinct from infra_failures(): this means "we don't know the job's
    outcome" (e.g. the runner App lacked Actions:Read and every lookup
    403'd), not "the job failed". Folding it into infra_failures would make
    an unrelated permissions problem look like a wave of real infra
    failures — exactly the kind of report a reader could act on wrongly.

    Returns None if the `conclusion` column is absent (pre-deploy
    snapshot) — same reasoning as infra_failures().

    `since` and `config_version` are independently combinable filters —
    passing both applies both (AND), not either-or.
    """
    if "conclusion" not in available_columns(conn):
        return None
    sql = (
        "SELECT COUNT(DISTINCT job_id) AS n FROM events "
        "WHERE kind='reap' AND conclusion = 'lookup_failed'"
    )
    params: list[float | str] = []
    if since is not None:
        sql += " AND ts > ?"
        params.append(since)
    if config_version is not None:
        sql += " AND config_version = ?"
        params.append(config_version)
    row = _rows(conn, sql, params).fetchone()
    return int(row["n"])


# --------------------------------------------------------------------------
# Rendering: everything below formats what the functions above compute. No
# function past this point invents a new statistic — it only reshapes
# gate_counts/binding_gate_counts/class_peaks/queue_waits/infra_failures
# output into markdown, filtered by `since` and/or `config_version` as those
# functions already support. The per-host helpers are the one exception:
# `host` isn't a parameter of the Task 5 functions, so a small amount of
# query logic for that one dimension lives here instead.
# --------------------------------------------------------------------------


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _data_window(
    conn: sqlite3.Connection, since: float | None
) -> tuple[float | None, float | None]:
    sql = "SELECT MIN(ts) AS min_ts, MAX(ts) AS max_ts FROM events"
    params: list[float] = []
    if since is not None:
        sql += " WHERE ts > ?"
        params.append(since)
    row = _rows(conn, sql, params).fetchone()
    return row["min_ts"], row["max_ts"]


def _distinct_hosts(conn: sqlite3.Connection, since: float | None) -> list[str]:
    """Distinct hosts recorded, or [] if this DB predates the `host` column.

    A pre-migration snapshot has no way to tell hosts apart, which is
    indistinguishable from "everything ran on one host" for this report's
    purposes — so the per-host section is simply omitted, same as it already
    is for any single-host dataset.
    """
    if "host" not in available_columns(conn):
        return []
    sql = "SELECT DISTINCT host FROM events WHERE host IS NOT NULL"
    params: list[float] = []
    if since is not None:
        sql += " AND ts > ?"
        params.append(since)
    return [row["host"] for row in _rows(conn, sql, params)]


def _host_summary(
    conn: sqlite3.Connection, since: float | None
) -> dict[str, dict[str, int | None]]:
    """Distinct reaped jobs and infra failures per host, for the per-host section.

    Only called once _distinct_hosts() has confirmed `host` exists. An infra
    failure here uses the same two-shape predicate as infra_failures() — a NULL
    conclusion OR the explicit 'infra_failure' sentinel (Task 13, emitted when a
    lane's host disappears mid-job) — and is gated behind the same
    _conclusion_validity_floor(). Without that floor, the ~4,000 pre-migration
    reap rows (conclusion NULL because the column didn't exist yet, not because
    anything failed) would all read as infra failures the first time a second
    host makes this table render at all.

    If `conclusion` is missing entirely (an unlikely partial-migration state,
    since metrics.py adds both columns in the same pass) or the floor cannot
    yet be derived (deployed, but no reap has landed since), infra_failures
    per host is reported as None rather than a wrong 0 — same reasoning as
    infra_failures() above.
    """
    has_conclusion = "conclusion" in available_columns(conn)
    floor = _conclusion_validity_floor(conn) if has_conclusion else None
    params: list[float] = []
    if floor is not None:
        infra_expr = (
            "COUNT(DISTINCT CASE WHEN (conclusion IS NULL OR conclusion = 'infra_failure') "
            "AND ts >= ? THEN job_id END)"
        )
        params.append(floor)
    else:
        infra_expr = "NULL"
    sql = (
        f"SELECT host, COUNT(DISTINCT job_id) AS jobs, {infra_expr} AS infra_failures "
        "FROM events WHERE kind='reap' AND host IS NOT NULL"
    )
    if since is not None:
        sql += " AND ts > ?"
        params.append(since)
    sql += " GROUP BY host"
    return {
        row["host"]: {"jobs": row["jobs"], "infra_failures": row["infra_failures"]}
        for row in _rows(conn, sql, params)
    }


def _host_admits(conn: sqlite3.Connection, since: float | None) -> dict[str, int]:
    """Distinct admitted jobs per host — one of the two numbers a second host is
    bought to move (the other is _host_binding_gate_counts())."""
    sql = (
        "SELECT host, COUNT(DISTINCT job_id) AS n FROM events "
        "WHERE kind='admit' AND host IS NOT NULL"
    )
    params: list[float] = []
    if since is not None:
        sql += " AND ts > ?"
        params.append(since)
    sql += " GROUP BY host"
    return {row["host"]: row["n"] for row in _rows(conn, sql, params)}


def _host_binding_gate_counts(
    conn: sqlite3.Connection, since: float | None
) -> dict[str, dict[str, int]]:
    """Per-host binding-gate counts — the masking-corrected view, broken down by host.

    Mirrors binding_gate_counts()'s logic (the reasons/reason fallback, the
    already_running exclusion, the distinct-job dedup) but groups by host. The
    caller MUST pass `since` from _binding_window(), the same reasons-validity
    floor the fleet-wide table uses — a per-host table built from pre-fix rows
    would reintroduce exactly the masking bias this plan exists to remove.
    """
    has_reasons = "reasons" in available_columns(conn)
    select = "host, job_id, reason, reasons" if has_reasons else "host, job_id, reason"
    sql = f"SELECT {select} FROM events WHERE kind='defer' AND host IS NOT NULL"
    params: list[float] = []
    if since is not None:
        sql += " AND ts > ?"
        params.append(since)
    seen: dict[str, dict[str, set[int]]] = {}
    for row in _rows(conn, sql, params):
        reasons_val = row["reasons"] if has_reasons else None
        gates = (reasons_val or row["reason"] or "").split(",")
        host = row["host"]
        for gate in gates:
            if gate and gate not in EXCLUDED_REASONS:
                seen.setdefault(host, {}).setdefault(gate, set()).add(row["job_id"])
    return {host: {gate: len(jobs) for gate, jobs in gates.items()} for host, gates in seen.items()}


def _effective_since(since: float | None, floor: float) -> float:
    """The later of a caller's `since` and a hard floor like PAGE_CACHE_FIX_TS."""
    return floor if since is None else max(since, floor)


def _binding_window(conn: sqlite3.Connection, since: float | None) -> tuple[float | None, bool]:
    """(effective since for gate counts, whether the binding view is trustworthy).

    Shared by both renderers: render_report and render_comparison must agree on when
    binding-gate counts are real, or the comparison hands back the masked view the
    main report refuses to show.
    """
    floor = _reasons_validity_floor(conn)
    if floor is None:
        return since, False
    # `since` filters with `ts > ?`, but the floor is a real row that must be INCLUDED.
    return _effective_since(since, math.nextafter(floor, float("-inf"))), True


def _render_peaks_table(peaks: dict[str, dict[str, int]]) -> list[str]:
    if not peaks:
        return ["No peak-RAM samples at or after the validity cutoff."]
    lines = ["| class | n | p50 | p90 | p95 | max |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for cls, stats in sorted(peaks.items()):
        lines.append(
            f"| {cls} | {stats['n']} | {stats['p50']} | {stats['p90']} | {stats['p95']} "
            f"| {stats['max']} |"
        )
    return lines


def _fmt_infra_failures(n: int | None) -> str:
    if n is None:
        return "unavailable — requires the `conclusion` column (controller redeploy pending)"
    return str(n)


def _split_by_eligibility(counts: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    """Split a gate_counts()/binding_gate_counts() dict into (capacity, eligibility).

    Keeps ELIGIBILITY_REASONS (e.g. not_allowlisted) out of the capacity-gate table so a
    reader can't mistake "this job is never served here" for "capacity tuning would help".
    """
    capacity = {g: n for g, n in counts.items() if g not in ELIGIBILITY_REASONS}
    eligibility = {g: n for g, n in counts.items() if g in ELIGIBILITY_REASONS}
    return capacity, eligibility


def _binding_unavailable_note() -> list[str]:
    """The explanatory line shown wherever a binding-gate view cannot be rendered.

    Shared verbatim by the fleet-wide Admission Gates section and the per-host
    Binding Gates by Host sub-table, so a reader sees one consistent explanation
    rather than two independently-worded (and possibly drifting) ones.
    """
    return [
        "**Binding-gate counts are unavailable: no defer row in this database has "
        "recorded `reasons` yet.** Either the column predates the additive migration on "
        "the controller's write path (see `services/ci-controller/src/metrics.py`), or the "
        "migration has run but the multi-gate controller has not yet deferred a job. The "
        "corrected, masking-free view cannot be shown yet; showing the primary-reason "
        "count in its place would understate the masking bias as zero, which is the wrong "
        "conclusion. Historical rows keep `reasons` NULL by design (no backfill), so this "
        "fills in as new defer events accumulate post-deploy.",
        "",
    ]


def _render_gate_table(
    primary: dict[str, int], binding: dict[str, int], binding_available: bool = True
) -> list[str]:
    gates = sorted(set(primary) | set(binding), key=lambda g: -binding.get(g, primary.get(g, 0)))
    if not gates:
        return ["No defer events in this window."]
    lines = ["| gate | primary-reason jobs | binding-gate jobs |", "| --- | ---: | ---: |"]
    for gate in gates:
        binding_cell = str(binding.get(gate, 0)) if binding_available else "unavailable"
        lines.append(f"| {gate} | {primary.get(gate, 0)} | {binding_cell} |")
    return lines


def _render_host_binding_gates(host_gates: dict[str, dict[str, int]]) -> list[str]:
    """Render the per-host binding-gate breakdown. Caller must have already windowed
    `host_gates` to _binding_window()'s floor — this function only formats."""
    if not host_gates:
        return ["No defer events with a recorded host at or after the reasons validity floor."]
    lines = ["| host | gate | binding-gate jobs |", "| --- | --- | ---: |"]
    for host in sorted(host_gates):
        for gate, n in sorted(host_gates[host].items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"| {host} | {gate} | {n} |")
    return lines


def _render_waits_table(waits: dict[str, list[float]]) -> list[str]:
    if not waits:
        return ["No completed admissions with a measurable queue wait in this window."]
    lines = ["| class | n | p50 | p90 | p95 |", "| --- | ---: | ---: | ---: | ---: |"]
    for cls, vals in sorted(waits.items()):
        ints = [round(v) for v in vals]
        lines.append(
            f"| {cls} | {len(ints)} | {percentile(ints, 50)} | {percentile(ints, 90)} "
            f"| {percentile(ints, 95)} |"
        )
    return lines


def render_report(
    conn: sqlite3.Connection,
    since: float | None = None,
    peaks_since: float = PAGE_CACHE_FIX_TS,
    invocation: str = "python -m src.ci_bench",
) -> str:
    """Render the full capacity report as markdown.

    peaks_since defaults to PAGE_CACHE_FIX_TS: peak-RAM figures older than
    that are unusable (they counted reclaimable page cache), so the report
    filters them out by default and states the cutoff so nobody re-quotes
    the pre-fix numbers.

    `invocation` is the exact command that produced this report (main() fills
    in the flags actually used, e.g. `--since-days`); this file is committed,
    so a reader must be able to reproduce it exactly, not just re-run the
    tool with unknown defaults.
    """
    lines = ["# CI Capacity Report", ""]
    min_ts, max_ts = _data_window(conn, since)
    lines.append(f"Generated by `{invocation}` (report) at {_fmt_ts(time.time())}.")

    if min_ts is None or max_ts is None:
        lines.append("")
        lines.append("Dataset is empty — no events recorded in the selected window.")
        return "\n".join(lines) + "\n"

    lines.append(f"Data window: {_fmt_ts(min_ts)} .. {_fmt_ts(max_ts)}.")
    lines.append("")

    reasons_floor = _reasons_validity_floor(conn)
    # Both columns MUST cover the same window. Flooring only the binding column would
    # compare a 47-day primary count against a 3-day binding count, making their gap —
    # the headline number — meaningless.
    gate_since, binding_available = _binding_window(conn, since)
    capacity_primary, eligibility_primary = _split_by_eligibility(
        gate_counts(conn, since=gate_since)
    )
    capacity_binding, eligibility_binding = _split_by_eligibility(
        binding_gate_counts(conn, since=gate_since)
    )

    lines.append("## Admission Gates")
    lines.append("")
    lines.append(
        "Primary-reason counts attribute a job to only its first gate; a job deferred for "
        "`lane_ceiling,budget_full` counts only under `lane_ceiling` there. Binding-gate "
        "counts attribute the job to every gate that applied. The gap between the two "
        "columns is an UPPER BOUND on the size of that masking bias, not the bias itself: "
        "both counts are per-job over the whole window, so a job that hit one gate on an "
        "early tick and a different gate on a later tick also widens the gap, without any "
        "single admission decision having been masked. "
        f"Eligibility reasons (currently: {', '.join(sorted(ELIGIBILITY_REASONS))}) are "
        "excluded from this table and reported separately below — no capacity tuning "
        "changes them."
    )
    lines.append("")
    if reasons_floor is None:
        lines += _binding_unavailable_note()
    else:
        lines.append(
            f"**Both columns cover defers at or after {_fmt_ts(reasons_floor)}**, the earliest "
            "row that recorded `reasons`. Rows before it carry only the masked single-gate "
            "view, so including them would blend masked and corrected data under the "
            "corrected label — and windowing only the binding column would make the gap "
            "between the two columns meaningless."
        )
        lines.append("")
    lines += _render_gate_table(
        capacity_primary,
        capacity_binding,
        binding_available=binding_available,
    )
    lines.append("")

    if eligibility_primary or eligibility_binding:
        lines.append("## Eligibility (not a capacity gate)")
        lines.append("")
        lines.append(
            "These reasons mean the job is not served here at all — e.g. its repo isn't "
            "allowlisted — regardless of free RAM, lanes, or disk. Kept out of the Admission "
            "Gates table above so an outlier no tuning can fix doesn't skew it; still counted "
            "because it says real jobs are queueing that this controller will never serve."
        )
        lines.append("")
        lines += _render_gate_table(
            eligibility_primary,
            eligibility_binding,
            binding_available=binding_available,
        )
        lines.append("")

    peaks_cutoff = _fmt_ts(peaks_since)
    lines.append("## Per-Class Peak RAM (post page-cache fix only)")
    lines.append("")
    lines.append(
        f"**peak_ram_mb recorded before {peaks_cutoff} counted reclaimable page cache and is "
        f"excluded from this table.** Only reap events at or after {peaks_cutoff} are included."
    )
    lines.append("")
    lines += _render_peaks_table(class_peaks(conn, since=_effective_since(since, peaks_since)))
    lines.append("")

    lines.append("## Queue Wait (seconds, first defer to admit)")
    lines.append("")
    lines += _render_waits_table(queue_waits(conn, since=since))
    lines.append("")

    lines.append("## Infra Failures")
    lines.append("")
    conclusion_floor = _conclusion_validity_floor(conn)
    if conclusion_floor is not None:
        lines.append(
            f"Counted only for reaps at or after {_fmt_ts(conclusion_floor)}, the earliest "
            "point this database recorded a `conclusion` value — rows before that are NULL "
            "because the column did not exist yet, not because anything failed."
        )
        lines.append("")
    lines.append(
        "Reaps with no resolvable conclusion: "
        f"{_fmt_infra_failures(infra_failures(conn, since=since))}."
    )
    lines.append(
        "Reaps where the conclusion lookup itself failed (exception swallowed, e.g. a "
        'missing GitHub App permission) — kept separate because it means "unknown", not '
        f'"failed": {_fmt_infra_failures(lookup_failures(conn, since=since))}.'
    )

    hosts = _distinct_hosts(conn, since=since)
    if len(hosts) > 1:
        lines.append("")
        lines.append("## Per-Host Breakdown")
        lines.append("")
        summary = _host_summary(conn, since=since)
        host_admits = _host_admits(conn, since=since)
        lines.append("| host | reaped jobs | admits | infra failures |")
        lines.append("| --- | ---: | ---: | ---: |")
        for host in sorted(summary):
            stats = summary[host]
            lines.append(
                f"| {host} | {stats['jobs']} | {host_admits.get(host, 0)} "
                f"| {_fmt_infra_failures(stats['infra_failures'])} |"
            )
        lines.append("")
        lines.append("### Binding Gates by Host")
        lines.append("")
        # Reuse the SAME reasons-validity floor as the fleet-wide gate table — a
        # per-host table built from pre-fix rows would reintroduce exactly the
        # masking bias this report exists to remove.
        host_gate_since, host_binding_available = _binding_window(conn, since)
        if not host_binding_available:
            lines += _binding_unavailable_note()
        else:
            lines += _render_host_binding_gates(
                _host_binding_gate_counts(conn, since=host_gate_since)
            )
            lines.append("")

    return "\n".join(lines) + "\n"


def render_comparison(conn: sqlite3.Connection, version_a: str, version_b: str) -> str:
    """Compare two config_version values: binding gates, class peaks, queue waits, infra failures.

    Peak-RAM figures are always restricted to PAGE_CACHE_FIX_TS onward here too — a config
    comparison that mixed a pre-fix and post-fix version would silently compare apples to
    reclaimable-cache-tainted oranges.
    """
    lines = [
        "# CI Config Comparison",
        "",
        f"Generated by `python -m src.ci_bench --compare {version_a} {version_b}` "
        f"at {_fmt_ts(time.time())}.",
        f"Peak-RAM figures below include only reap events at or after {_fmt_ts(PAGE_CACHE_FIX_TS)} "
        "(the page-cache fix); see PAGE_CACHE_FIX_TS.",
        "",
    ]
    gate_since, binding_available = _binding_window(conn, None)
    for version in (version_a, version_b):
        lines.append(f"## config_version = {version}")
        lines.append("")
        lines.append("### Binding Gates")
        lines.append("")
        if not binding_available:
            lines.append(
                "**Binding-gate counts are unavailable: no defer row has recorded `reasons` "
                "yet.** They are NOT shown below, because falling back to each row's single "
                "primary reason would hand back the masked view this report exists to "
                "retire. See the Admission Gates note in the main capacity report."
            )
            lines.append("")
        # Only the binding-gate tables depend on `reasons`. Peaks, queue wait and infra
        # failures come from reap/admit rows and stay valid — skipping the whole section
        # would gut --compare on any pre-deploy database.
        capacity_binding, eligibility_binding = (
            _split_by_eligibility(
                binding_gate_counts(conn, since=gate_since, config_version=version)
            )
            if binding_available
            else ({}, {})
        )
        if capacity_binding:
            lines.append("| gate | binding-gate jobs |")
            lines.append("| --- | ---: |")
            for gate, n in capacity_binding.items():
                lines.append(f"| {gate} | {n} |")
        elif binding_available:
            lines.append("No defer events for this config_version.")
        lines.append("")
        if eligibility_binding:
            lines.append("### Eligibility (not a capacity gate)")
            lines.append("")
            lines.append(
                "These reasons mean the job is not served here at all — no capacity tuning "
                "changes them. See the Admission Gates note in the main capacity report."
            )
            lines.append("")
            lines.append("| gate | binding-gate jobs |")
            lines.append("| --- | ---: |")
            for gate, n in eligibility_binding.items():
                lines.append(f"| {gate} | {n} |")
            lines.append("")
        lines.append("### Per-Class Peak RAM")
        lines.append("")
        lines += _render_peaks_table(
            class_peaks(conn, since=PAGE_CACHE_FIX_TS, config_version=version)
        )
        lines.append("")
        lines.append("### Queue Wait (seconds, first defer to admit)")
        lines.append("")
        lines += _render_waits_table(queue_waits(conn, config_version=version))
        lines.append("")
        lines.append("### Infra Failures")
        lines.append("")
        lines.append(
            "Reaps with no resolvable conclusion: "
            f"{_fmt_infra_failures(infra_failures(conn, config_version=version))}."
        )
        lines.append(
            "Reaps where the conclusion lookup itself failed (kept separate — see the "
            "Infra Failures note in the main capacity report): "
            f"{_fmt_infra_failures(lookup_failures(conn, config_version=version))}."
        )
        lines.append("")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ci-bench",
        description="Render the CI capacity report from the controller's events log.",
    )
    parser.add_argument(
        "--db",
        default="/var/lib/ci-controller/metrics.db",
        help="Path to the controller's events SQLite DB (read-only).",
    )
    parser.add_argument(
        "--since-days",
        type=float,
        default=None,
        help="Only include events from the last N days.",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("VERSION_A", "VERSION_B"),
        help="Compare two config_version values instead of rendering the full report.",
    )
    parser.add_argument(
        "--md",
        action="store_true",
        help="No-op: markdown is the only output format ci-bench produces.",
    )
    args = parser.parse_args(argv)

    try:
        conn = open_readonly(args.db)
    except sqlite3.OperationalError:
        print(f"ci-bench: could not open database at {args.db!r}", file=sys.stderr)
        return 1

    try:
        if args.compare:
            version_a, version_b = args.compare
            output = render_comparison(conn, version_a, version_b)
        else:
            since = time.time() - args.since_days * 86400 if args.since_days is not None else None
            # `make ci-report` is the reproducible entry point: it snapshots the live DB
            # to a path (/tmp/ci-metrics.db) the Makefile deletes afterwards, so naming
            # that path in the committed report would point a reader at a file that no
            # longer exists. Name the make target instead. --since-days isn't a make
            # target flag, so that case names the literal command that produced it —
            # reproducible for anyone holding the same DB snapshot.
            if args.since_days is not None:
                invocation = f"python -m src.ci_bench --db {args.db} --since-days {args.since_days}"
            else:
                invocation = "make ci-report"
            output = render_report(conn, since=since, invocation=invocation)
    except sqlite3.OperationalError as exc:
        print(f"ci-bench: database error while reading {args.db!r}: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
