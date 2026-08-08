import sqlite3
import time

from src.ci_bench import (
    ELIGIBILITY_REASONS,
    PAGE_CACHE_FIX_TS,
    available_columns,
    binding_gate_counts,
    class_peaks,
    gate_counts,
    infra_failures,
    lookup_failures,
    main,
    open_readonly,
    percentile,
    queue_waits,
    render_comparison,
    render_report,
)
from src.metrics import MetricsStore


def _store(tmp_path) -> MetricsStore:
    return MetricsStore(str(tmp_path / "m.db"))


def _old_schema_db(tmp_path, rows: list[tuple]) -> sqlite3.Connection:
    """Build a DB with the pre-Task-2, 12-column events table (no reasons,
    job_name, workflow, host, conclusion) and open it read-only, mirroring
    what ci-bench sees when pointed at a controller that hasn't been
    redeployed yet. Construction matches
    tests/test_metrics.py::test_migrates_a_preexisting_db_without_losing_rows.

    `rows` are (job_id, kind, reason, ts, config_version, class_name) tuples.
    """
    db = tmp_path / "old.db"
    old = sqlite3.connect(str(db))
    old.executescript(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL, kind TEXT NOT NULL, job_id INTEGER NOT NULL,
            repo TEXT NOT NULL, class TEXT, work_disk TEXT, reason TEXT,
            config_version TEXT NOT NULL, lane_id TEXT,
            peak_ram_mb INTEGER, peak_cpu_pct REAL
        );
        """
    )
    for job_id, kind, reason, ts, config_version, class_name in rows:
        old.execute(
            "INSERT INTO events (ts, kind, job_id, repo, reason, config_version, class) "
            "VALUES (?, ?, ?, 'o/r', ?, ?, ?)",
            (ts, kind, job_id, reason, config_version, class_name),
        )
    old.commit()
    old.close()
    return open_readonly(str(db))


def test_percentile_interpolates() -> None:
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([10, 20], 100) == 20
    assert percentile([], 50) is None


def test_binding_gates_are_not_masked_by_the_primary_reason(tmp_path) -> None:
    """The whole point: primary counts undercount budget_full; binding counts don't."""
    store = _store(tmp_path)
    for job_id in (1, 2):
        store.record_event(
            kind="defer",
            job_id=job_id,
            repo="o/r",
            ts=float(job_id),
            config_version="v",
            reason="lane_ceiling",
            reasons="lane_ceiling,budget_full",
        )
    conn = store.conn

    assert gate_counts(conn) == {"lane_ceiling": 2}
    assert binding_gate_counts(conn) == {"lane_ceiling": 2, "budget_full": 2}
    store.close()


def test_already_running_is_excluded_from_gate_statistics(tmp_path) -> None:
    """already_running means "this job already has a lane" — not a capacity gate."""
    store = _store(tmp_path)
    store.record_event(
        kind="defer",
        job_id=1,
        repo="o/r",
        ts=1.0,
        config_version="v",
        reason="already_running",
        reasons="already_running",
    )
    store.record_event(
        kind="defer",
        job_id=2,
        repo="o/r",
        ts=2.0,
        config_version="v",
        reason="budget_full",
        reasons="budget_full,already_running",
    )
    conn = store.conn

    assert "already_running" not in gate_counts(conn)
    assert "already_running" not in binding_gate_counts(conn)
    assert binding_gate_counts(conn) == {"budget_full": 1}
    store.close()


def test_binding_gate_counts_falls_back_to_reason_for_pre_branch_rows(tmp_path) -> None:
    """All ~112,000 pre-Task-4 rows have reasons=NULL; without the fallback
    they'd silently read as zero, which is the exact failure this module
    exists to prevent."""
    store = _store(tmp_path)
    store.record_event(
        kind="defer",
        job_id=1,
        repo="o/r",
        ts=1.0,
        config_version="v",
        reason="lane_ceiling",
        # reasons intentionally left unset (NULL) — the pre-branch row shape.
    )
    assert binding_gate_counts(store.conn) == {"lane_ceiling": 1}
    store.close()


def test_a_job_deferred_many_times_counts_once(tmp_path) -> None:
    """A queued job re-defers every tick; tick counts overstate demand ~17x."""
    store = _store(tmp_path)
    for tick in range(20):
        store.record_event(
            kind="defer",
            job_id=1,
            repo="o/r",
            ts=float(tick),
            config_version="v",
            reason="budget_full",
            reasons="budget_full",
        )
    assert binding_gate_counts(store.conn) == {"budget_full": 1}
    store.close()


def test_class_peaks_report_percentiles(tmp_path) -> None:
    store = _store(tmp_path)
    for i, peak in enumerate([100, 200, 300, 5000]):
        store.record_event(
            kind="reap",
            job_id=i,
            repo="o/r",
            ts=float(i),
            config_version="v",
            class_name="node",
            peak_ram_mb=peak,
            conclusion="success",
        )
    stats = class_peaks(store.conn)["node"]
    assert stats["n"] == 4
    assert stats["max"] == 5000
    store.close()


def test_queue_wait_measures_first_defer_to_admit(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_event(
        kind="defer",
        job_id=1,
        repo="o/r",
        ts=100.0,
        config_version="v",
        reason="budget_full",
        reasons="budget_full",
        class_name="light",
    )
    store.record_event(
        kind="defer",
        job_id=1,
        repo="o/r",
        ts=115.0,
        config_version="v",
        reason="budget_full",
        reasons="budget_full",
        class_name="light",
    )
    store.record_event(
        kind="admit", job_id=1, repo="o/r", ts=160.0, config_version="v", class_name="light"
    )
    assert queue_waits(store.conn)["light"] == [60.0]
    store.close()


def test_queue_wait_uses_the_true_first_defer_even_when_it_precedes_the_since_window(
    tmp_path,
) -> None:
    """IMPORTANT 2: a job re-defers every poll tick. If the same `since` filter is applied
    to the MIN(ts) defer lookup as to the admit lookup, a job first deferred BEFORE the
    window that keeps re-deferring INSIDE it gets its wait computed from the earliest
    in-window re-defer, not its true first defer — understating the wait as
    `admit_ts - window_start`. This biases the longest waits (p90/p95) hardest."""
    store = _store(tmp_path)
    store.record_event(
        kind="defer",
        job_id=1,
        repo="o/r",
        ts=10.0,  # before the since=100.0 window below
        config_version="v",
        reason="budget_full",
        reasons="budget_full",
        class_name="light",
    )
    store.record_event(
        kind="defer",
        job_id=1,
        repo="o/r",
        ts=500.0,  # a re-defer inside the window
        config_version="v",
        reason="budget_full",
        reasons="budget_full",
        class_name="light",
    )
    store.record_event(
        kind="admit", job_id=1, repo="o/r", ts=600.0, config_version="v", class_name="light"
    )
    # True wait: 600 - 10 = 590. The old (buggy) behaviour would report 600 - 500 = 100.
    assert queue_waits(store.conn, since=100.0)["light"] == [590.0]
    store.close()


def test_queue_wait_applies_the_config_version_filter_only_to_the_admit_side(tmp_path) -> None:
    """Same reasoning as the since-window case: a job's true first defer may have
    happened under an earlier config_version than the one it was ultimately admitted
    under. Filtering the first-defer lookup by config_version would understate the wait
    the same way filtering it by `since` does."""
    store = _store(tmp_path)
    store.record_event(
        kind="defer",
        job_id=1,
        repo="o/r",
        ts=10.0,
        config_version="v1",
        reason="budget_full",
        reasons="budget_full",
        class_name="light",
    )
    store.record_event(
        kind="defer",
        job_id=1,
        repo="o/r",
        ts=500.0,
        config_version="v2",
        reason="budget_full",
        reasons="budget_full",
        class_name="light",
    )
    store.record_event(
        kind="admit", job_id=1, repo="o/r", ts=600.0, config_version="v2", class_name="light"
    )
    assert queue_waits(store.conn, config_version="v2")["light"] == [590.0]
    store.close()


def test_infra_failure_is_a_reap_without_a_conclusion(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_event(
        kind="reap",
        job_id=1,
        repo="o/r",
        ts=1.0,
        config_version="v",
        class_name="light",
        conclusion="success",
    )
    store.record_event(
        kind="reap",
        job_id=2,
        repo="o/r",
        ts=2.0,
        config_version="v",
        class_name="light",
        conclusion=None,
    )
    assert infra_failures(store.conn) == 1
    store.close()


def test_infra_failures_excludes_reap_rows_from_before_the_conclusion_column_existed(
    tmp_path,
) -> None:
    """CRITICAL 1(a): rows written before the controller wrote `conclusion` have it NULL
    because the column didn't exist yet, not because anything failed. Counting them would
    report ~4,000 false infra failures on the first post-deploy report. The floor is
    derived from the data (the earliest reap ts with a non-NULL conclusion), not a fixed
    date, since it depends on when the operator deploys."""
    store = _store(tmp_path)
    # "Pre-deploy" rows: conclusion left unset, simulating history written before the
    # column existed.
    for job_id in (1, 2, 3):
        store.record_event(
            kind="reap",
            job_id=job_id,
            repo="o/r",
            ts=float(job_id),
            config_version="v",
            class_name="light",
        )
    # "Post-deploy": the controller starts writing conclusion. job 4 succeeded; job 5 is
    # a genuine infra failure recorded after the floor.
    store.record_event(
        kind="reap",
        job_id=4,
        repo="o/r",
        ts=100.0,
        config_version="v",
        class_name="light",
        conclusion="success",
    )
    store.record_event(
        kind="reap",
        job_id=5,
        repo="o/r",
        ts=101.0,
        config_version="v",
        class_name="light",
        conclusion=None,
    )
    # Naive counting (no floor) would give 4 (jobs 1, 2, 3, 5). Only job 5 is real.
    assert infra_failures(store.conn) == 1
    store.close()


def test_infra_failures_is_none_when_no_row_has_ever_recorded_a_conclusion(tmp_path) -> None:
    """The `conclusion` column exists (post-migration) but no reap has landed since
    deploy yet — the validity floor can't be derived, so the count must stay unknown
    rather than silently reporting 0 (which would claim "no failures observed")."""
    store = _store(tmp_path)
    store.record_event(
        kind="reap", job_id=1, repo="o/r", ts=1.0, config_version="v", class_name="light"
    )
    assert infra_failures(store.conn) is None
    store.close()


def test_lookup_failures_are_counted_separately_from_infra_failures(tmp_path) -> None:
    """CRITICAL 1(b): a swallowed lookup exception ('lookup_failed') must not be folded
    into infra_failures — it means "we don't know", not "it failed"."""
    store = _store(tmp_path)
    store.record_event(
        kind="reap",
        job_id=1,
        repo="o/r",
        ts=1.0,
        config_version="v",
        class_name="light",
        conclusion="lookup_failed",
    )
    store.record_event(
        kind="reap",
        job_id=2,
        repo="o/r",
        ts=2.0,
        config_version="v",
        class_name="light",
        conclusion=None,
    )
    assert lookup_failures(store.conn) == 1
    assert infra_failures(store.conn) == 1  # only job 2; lookup_failed must not count here
    store.close()


def test_adopted_lanes_are_not_counted_as_infra_failures(tmp_path) -> None:
    """The '(adopted)' sentinel means "lookup skipped by design", not "genuinely no
    conclusion" — it must not inflate infra_failures either."""
    store = _store(tmp_path)
    store.record_event(
        kind="reap",
        job_id=1,
        repo="(adopted)",
        ts=1.0,
        config_version="v",
        class_name="light",
        conclusion="adopted",
    )
    store.record_event(
        kind="reap",
        job_id=2,
        repo="o/r",
        ts=2.0,
        config_version="v",
        class_name="light",
        conclusion=None,
    )
    assert infra_failures(store.conn) == 1
    store.close()


def test_report_states_the_peak_validity_window(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_event(
        kind="reap",
        job_id=1,
        repo="o/r",
        ts=1.0,
        config_version="v",
        class_name="node",
        peak_ram_mb=99999,
        conclusion="success",
    )
    report = render_report(store.conn)
    assert "2026-07-30" in report
    assert "99999" not in report  # a pre-fix peak must never reach the table
    store.close()


def test_report_states_the_conclusion_validity_window(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_event(
        kind="reap",
        job_id=1,
        repo="o/r",
        ts=1723100000.0,
        config_version="v",
        class_name="light",
        conclusion="success",
    )
    report = render_report(store.conn)
    assert "2024-08-08" in report  # datetime.fromtimestamp(1723100000, UTC) formatted date
    assert "did not exist yet" in report
    store.close()


def test_report_shows_lookup_failed_separately_from_infra_failures(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_event(
        kind="reap",
        job_id=1,
        repo="o/r",
        ts=1.0,
        config_version="v",
        class_name="light",
        conclusion="lookup_failed",
    )
    report = render_report(store.conn)
    infra_line, _, lookup_line = report.partition("Reaps where the conclusion lookup itself")
    assert "Reaps with no resolvable conclusion: 0." in infra_line
    assert "1." in lookup_line.splitlines()[0]
    store.close()


def test_report_gap_wording_calls_it_an_upper_bound(tmp_path) -> None:
    """IMPORTANT 6: both counts are per-job over the whole window, so a job that hit two
    different gates on two different ticks widens the gap without any single admission
    decision having been masked — the gap is an upper bound, not the bias itself."""
    store = _store(tmp_path)
    store.record_event(
        kind="defer",
        job_id=1,
        repo="o/r",
        ts=1.0,
        config_version="v",
        reason="lane_ceiling",
        reasons="lane_ceiling,budget_full",
    )
    report = render_report(store.conn)
    assert "upper bound" in report.lower()
    assert "is the size of that masking bias —" not in report
    store.close()


def test_report_shows_both_primary_and_binding_gate_counts(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_event(
        kind="defer",
        job_id=1,
        repo="o/r",
        ts=1.0,
        config_version="v",
        reason="lane_ceiling",
        reasons="lane_ceiling,budget_full",
    )
    report = render_report(store.conn)
    assert "lane_ceiling" in report and "budget_full" in report
    store.close()


def test_report_keeps_not_allowlisted_out_of_the_capacity_gate_table(tmp_path) -> None:
    """not_allowlisted means "this repo isn't served here" — an eligibility check, not a
    capacity gate. It must not appear in the Admission Gates table (which a reader would
    reasonably read as "capacity tuning could fix this"), but it must still be counted
    somewhere: it says real jobs are queueing that this controller will never serve."""
    store = _store(tmp_path)
    store.record_event(
        kind="defer",
        job_id=1,
        repo="o/r",
        ts=1.0,
        config_version="v",
        reason="lane_ceiling",
        reasons="lane_ceiling,budget_full",
    )
    store.record_event(
        kind="defer",
        job_id=2,
        repo="untracked/repo",
        ts=2.0,
        config_version="v",
        reason="not_allowlisted",
        reasons="not_allowlisted",
    )
    report = render_report(store.conn)

    admission_gates, _, rest = report.partition("## Eligibility")
    # No *row* for not_allowlisted in the capacity table — the gate name may still appear
    # in the section's explanatory prose (naming which reasons are excluded and why).
    assert "| not_allowlisted |" not in admission_gates
    assert "| lane_ceiling |" in admission_gates and "| budget_full |" in admission_gates

    eligibility_section = "## Eligibility" + rest
    assert "| not_allowlisted | 1 | 1 |" in eligibility_section
    store.close()


def test_gate_counts_still_include_not_allowlisted(tmp_path) -> None:
    """The stats functions keep counting it — only the report's rendering separates it
    from capacity gates. ELIGIBILITY_REASONS is a rendering concern, not an exclusion
    list like EXCLUDED_REASONS."""
    store = _store(tmp_path)
    store.record_event(
        kind="defer",
        job_id=1,
        repo="untracked/repo",
        ts=1.0,
        config_version="v",
        reason="not_allowlisted",
        reasons="not_allowlisted",
    )
    assert gate_counts(store.conn) == {"not_allowlisted": 1}
    assert binding_gate_counts(store.conn) == {"not_allowlisted": 1}
    assert {"not_allowlisted"} == ELIGIBILITY_REASONS
    store.close()


def test_compare_reports_both_config_versions(tmp_path) -> None:
    store = _store(tmp_path)
    for job_id, version in ((1, "aaa"), (2, "bbb")):
        store.record_event(
            kind="defer",
            job_id=job_id,
            repo="o/r",
            ts=1.0,
            config_version=version,
            reason="budget_full",
            reasons="budget_full",
        )
    out = render_comparison(store.conn, "aaa", "bbb")
    assert "aaa" in out and "bbb" in out
    store.close()


def test_report_on_empty_database_does_not_raise(tmp_path) -> None:
    store = _store(tmp_path)
    report = render_report(store.conn)
    assert "empty" in report.lower()
    assert "None" not in report
    store.close()


def test_report_omits_host_section_when_host_is_uniformly_null(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_event(
        kind="reap",
        job_id=1,
        repo="o/r",
        ts=2000000000.0,
        config_version="v",
        class_name="node",
        peak_ram_mb=100,
        conclusion="success",
    )
    report = render_report(store.conn)
    # MINOR 8: "host" alone is a bad probe — it also appears in "host_pressure" (a real
    # gate name shown elsewhere in the report). Assert on the actual section heading.
    assert "## Per-Host Breakdown" not in report
    store.close()


def test_report_includes_host_section_when_multiple_hosts_present(tmp_path) -> None:
    store = _store(tmp_path)
    for job_id, host in ((1, "powerserver"), (2, "second-host")):
        store.record_event(
            kind="reap",
            job_id=job_id,
            repo="o/r",
            ts=2000000000.0,
            config_version="v",
            class_name="node",
            peak_ram_mb=100,
            conclusion="success",
            host=host,
        )
    report = render_report(store.conn)
    assert "powerserver" in report and "second-host" in report
    store.close()


def test_report_generated_by_header_names_command_and_window(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_event(
        kind="reap",
        job_id=1,
        repo="o/r",
        ts=2000000000.0,
        config_version="v",
        class_name="node",
        peak_ram_mb=100,
        conclusion="success",
    )
    report = render_report(store.conn)
    assert "python -m src.ci_bench" in report
    store.close()


def test_cli_report_writes_markdown_to_stdout(tmp_path, capsys) -> None:
    db_path = tmp_path / "m.db"
    store = MetricsStore(str(db_path))
    store.record_event(
        kind="reap",
        job_id=1,
        repo="o/r",
        ts=2000000000.0,
        config_version="v",
        class_name="node",
        peak_ram_mb=100,
        conclusion="success",
    )
    store.close()
    rc = main(["--db", str(db_path), "--md"])
    assert rc == 0
    out = capsys.readouterr().out
    # MINOR 7: the committed report must name a reproducible command. `--db
    # /tmp/ci-metrics.db` (the Makefile's own snapshot path) is deleted right after
    # generation, so pointing a reader at it would be unreproducible; `make ci-report`
    # is the entry point that actually works.
    assert "make ci-report" in out


def test_cli_compare_flag(tmp_path, capsys) -> None:
    db_path = tmp_path / "m.db"
    store = MetricsStore(str(db_path))
    for job_id, version in ((1, "aaa"), (2, "bbb")):
        store.record_event(
            kind="defer",
            job_id=job_id,
            repo="o/r",
            ts=1.0,
            config_version=version,
            reason="budget_full",
            reasons="budget_full",
        )
    store.close()
    rc = main(["--db", str(db_path), "--compare", "aaa", "bbb"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "aaa" in out and "bbb" in out


def test_per_version_stats_agree_with_whole_dataset_when_one_version_present(tmp_path) -> None:
    """Guards against binding_gate_counts/class_peaks/queue_waits/infra_failures drifting
    from a hand-duplicated per-version implementation: with a single config_version in
    the fixture, filtering by that version must return exactly what filtering by nothing
    returns — a second, independently-maintained copy of this logic could silently
    disagree on edge cases (already_running exclusion, the reasons/reason fallback, the
    distinct-job dedup) without any test catching it."""
    store = _store(tmp_path)
    store.record_event(
        kind="defer",
        job_id=1,
        repo="o/r",
        ts=PAGE_CACHE_FIX_TS + 1,
        config_version="only",
        reason="lane_ceiling",
        reasons="lane_ceiling,budget_full",
    )
    store.record_event(
        kind="reap",
        job_id=2,
        repo="o/r",
        ts=PAGE_CACHE_FIX_TS + 2,
        config_version="only",
        class_name="node",
        peak_ram_mb=1234,
        conclusion=None,
    )
    store.record_event(
        kind="defer",
        job_id=3,
        repo="o/r",
        ts=PAGE_CACHE_FIX_TS + 3,
        config_version="only",
        class_name="node",
    )
    store.record_event(
        kind="admit",
        job_id=3,
        repo="o/r",
        ts=PAGE_CACHE_FIX_TS + 30,
        config_version="only",
        class_name="node",
    )
    conn = store.conn

    assert binding_gate_counts(conn, config_version="only") == binding_gate_counts(conn)
    assert class_peaks(conn, config_version="only") == class_peaks(conn)
    assert queue_waits(conn, config_version="only") == queue_waits(conn)
    assert infra_failures(conn, config_version="only") == infra_failures(conn)
    store.close()


def test_binding_gate_counts_combines_since_and_config_version_filters(tmp_path) -> None:
    """since and config_version must AND together, not override one another."""
    store = _store(tmp_path)
    store.record_event(
        kind="defer",
        job_id=1,
        repo="o/r",
        ts=1.0,
        config_version="v1",
        reason="budget_full",
        reasons="budget_full",
    )
    store.record_event(
        kind="defer",
        job_id=2,
        repo="o/r",
        ts=100.0,
        config_version="v1",
        reason="budget_full",
        reasons="budget_full",
    )
    store.record_event(
        kind="defer",
        job_id=3,
        repo="o/r",
        ts=100.0,
        config_version="v2",
        reason="budget_full",
        reasons="budget_full",
    )
    conn = store.conn
    assert binding_gate_counts(conn, since=50.0, config_version="v1") == {"budget_full": 1}
    store.close()


def test_cli_missing_db_prints_one_line_to_stderr_and_exits_nonzero(tmp_path, capsys) -> None:
    missing = tmp_path / "does-not-exist.db"
    rc = main(["--db", str(missing)])
    assert rc != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(missing) in captured.err
    # exactly one line: no traceback leaked to the operator
    assert captured.err.count("\n") == 1


def test_cli_malformed_db_prints_one_line_to_stderr_and_exits_nonzero(tmp_path, capsys) -> None:
    """IMPORTANT 3: a DB that opens fine (valid sqlite file) but has no `events` table —
    e.g. an empty snapshot left by a failed ssh/ansible step — must degrade the same way
    a missing DB does, not traceback with sqlite3.OperationalError. main() only guarded
    open_readonly(); _data_window() (called from inside render_report) can still raise."""
    db_path = tmp_path / "empty.db"
    sqlite3.connect(str(db_path)).close()  # valid sqlite file, but no events table
    rc = main(["--db", str(db_path)])
    assert rc != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1


def test_cli_since_days_is_named_in_the_generated_by_header(tmp_path, capsys) -> None:
    db_path = tmp_path / "m.db"
    store = MetricsStore(str(db_path))
    store.record_event(
        kind="reap",
        job_id=1,
        repo="o/r",
        ts=time.time(),
        config_version="v",
        class_name="node",
        peak_ram_mb=100,
        conclusion="success",
    )
    store.close()
    rc = main(["--db", str(db_path), "--since-days", "7"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--since-days 7" in out


# --- Un-migrated (pre-Task-2, 12-column) database: ci-bench opens read-only, so it can
# never run metrics.py's additive migration itself. A snapshot pulled from a controller
# that hasn't been redeployed yet must degrade, not traceback with "no such column".


def test_available_columns_omits_columns_absent_from_an_old_schema(tmp_path) -> None:
    conn = _old_schema_db(tmp_path, rows=[])
    columns = available_columns(conn)
    assert "reasons" not in columns
    assert "conclusion" not in columns
    assert "host" not in columns
    assert {"id", "ts", "kind", "job_id", "reason", "config_version"} <= columns
    conn.close()


def test_binding_gate_counts_falls_back_when_reasons_column_is_entirely_absent(
    tmp_path,
) -> None:
    """Not just NULL — the column doesn't exist at all. Must not raise, and must still
    return correct primary-gate counts from `reason` alone."""
    conn = _old_schema_db(
        tmp_path,
        rows=[
            (1, "defer", "lane_ceiling", 1.0, "v", None),
            (2, "defer", "budget_full", 2.0, "v", None),
        ],
    )
    assert binding_gate_counts(conn) == {"lane_ceiling": 1, "budget_full": 1}
    assert binding_gate_counts(conn) == gate_counts(conn)
    conn.close()


def test_infra_failures_is_none_when_conclusion_column_is_absent(tmp_path) -> None:
    """0 would claim 'no failures observed'; the truth is 'cannot tell yet' — those are
    different claims and must not be conflated."""
    conn = _old_schema_db(tmp_path, rows=[(1, "reap", None, 1.0, "v", "node")])
    assert infra_failures(conn) is None
    conn.close()


def test_report_renders_without_raising_against_an_old_schema_db(tmp_path) -> None:
    conn = _old_schema_db(
        tmp_path,
        rows=[
            (1, "defer", "lane_ceiling", 1.0, "v", None),
            (2, "reap", None, PAGE_CACHE_FIX_TS + 10, "v", "node"),
        ],
    )
    report = render_report(conn)
    assert "lane_ceiling" in report
    conn.close()


def test_report_marks_binding_gate_column_unavailable_on_an_old_schema_db(tmp_path) -> None:
    """The whole point of this fix: an old-schema report must not silently print the
    primary count as the binding count, which would understate the masking bias as zero —
    the exact wrong conclusion this plan exists to correct."""
    conn = _old_schema_db(
        tmp_path,
        rows=[(1, "defer", "lane_ceiling", 1.0, "v", None)],
    )
    report = render_report(conn)
    assert "unavailable" in report.lower()
    assert "| lane_ceiling | 1 | unavailable |" in report
    conn.close()


def test_report_marks_infra_failures_unavailable_on_an_old_schema_db(tmp_path) -> None:
    conn = _old_schema_db(tmp_path, rows=[(1, "reap", None, 1.0, "v", "node")])
    report = render_report(conn)
    assert "Reaps with no resolvable conclusion: unavailable" in report
    conn.close()


def test_binding_counts_are_unavailable_until_a_row_has_recorded_reasons(tmp_path) -> None:
    """The column existing is not the same as the data being valid.

    After the migration adds `reasons`, historical rows still have it NULL and
    binding_gate_counts falls back to `reason` for them. Reporting that blend as
    "binding-gate jobs" would reintroduce the masking bias the report exists to expose.
    """
    store = _store(tmp_path)  # fresh schema: `reasons` column exists
    store.record_event(
        kind="defer", job_id=1, repo="o/r", ts=100.0, config_version="v", reason="lane_ceiling"
    )  # historical shape: reasons left NULL
    report = render_report(store.conn)
    # The gate row itself must read unavailable, not just some other section.
    assert "| lane_ceiling | 1 | unavailable |" in report
    store.close()


def test_gate_table_windows_both_columns_to_the_reasons_floor(tmp_path) -> None:
    """Primary and binding must cover the SAME window, or their gap is meaningless."""
    store = _store(tmp_path)
    # Pre-floor: masked historical row, reasons NULL.
    store.record_event(
        kind="defer", job_id=1, repo="o/r", ts=100.0, config_version="v", reason="disk_full"
    )
    # At/after the floor: real multi-gate rows.
    store.record_event(
        kind="defer",
        job_id=2,
        repo="o/r",
        ts=200.0,
        config_version="v",
        reason="lane_ceiling",
        reasons="lane_ceiling,budget_full",
    )
    report = render_report(store.conn)

    assert "| lane_ceiling | 1 | 1 |" in report  # both columns populated, same window
    # job_id=1's disk_full predates the floor and must not appear in either column.
    assert "disk_full" not in report
    # job_id=2 appears in both columns.
    assert "lane_ceiling" in report and "budget_full" in report
    store.close()


def test_reasons_validity_floor_window_is_stated_in_the_report(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_event(
        kind="defer",
        job_id=2,
        repo="o/r",
        ts=1785542400.0,
        config_version="v",
        reason="lane_ceiling",
        reasons="lane_ceiling,budget_full",
    )
    report = render_report(store.conn)
    # 2026-08-01, distinct from the page-cache cutoff so this can't pass by coincidence.
    assert "2026-08-01" in report
    store.close()


def test_comparison_does_not_label_masked_fallback_counts_as_binding(tmp_path) -> None:
    """The migrated-but-no-new-defers state: column exists, every `reasons` is NULL.

    render_comparison must not echo primary counts under a "Binding Gates" heading —
    that is the biased view this PR retires, handed back to the operator as if corrected.
    """
    store = _store(tmp_path)  # fresh schema: `reasons` exists
    for job_id, version in ((1, "aaa"), (2, "bbb")):
        store.record_event(
            kind="defer",
            job_id=job_id,
            repo="o/r",
            ts=100.0 + job_id,
            config_version=version,
            reason="budget_full",  # reasons left NULL
        )

    out = render_comparison(store.conn, "aaa", "bbb")

    assert "unavailable" in out.lower()
    assert "| budget_full | 1 |" not in out  # must not present the masked count as binding
    store.close()


def test_comparison_windows_binding_counts_to_the_reasons_floor(tmp_path) -> None:
    store = _store(tmp_path)
    # Pre-floor masked row for version aaa.
    store.record_event(
        kind="defer", job_id=1, repo="o/r", ts=100.0, config_version="aaa", reason="disk_full"
    )
    # At/after the floor, real multi-gate rows for both versions.
    store.record_event(
        kind="defer",
        job_id=2,
        repo="o/r",
        ts=200.0,
        config_version="aaa",
        reason="lane_ceiling",
        reasons="lane_ceiling,budget_full",
    )
    store.record_event(
        kind="defer",
        job_id=3,
        repo="o/r",
        ts=300.0,
        config_version="bbb",
        reason="budget_full",
        reasons="budget_full",
    )

    out = render_comparison(store.conn, "aaa", "bbb")

    assert "disk_full" not in out  # predates the floor
    assert "lane_ceiling" in out and "budget_full" in out
    store.close()


def test_comparison_still_renders_metrics_that_do_not_depend_on_reasons(tmp_path) -> None:
    """Degrading the binding view must not drop peaks, queue wait, or infra failures.

    Those come from reap/admit rows and are unaffected by whether `reasons` was ever
    written; dropping them would gut --compare on any pre-deploy database.
    """
    store = _store(tmp_path)
    store.record_event(
        kind="defer", job_id=1, repo="o/r", ts=100.0, config_version="aaa", reason="budget_full"
    )  # reasons NULL -> binding view unavailable
    store.record_event(
        kind="reap",
        job_id=1,
        repo="o/r",
        ts=1785542400.0,
        config_version="aaa",
        class_name="node",
        peak_ram_mb=4242,
        conclusion="success",
    )

    out = render_comparison(store.conn, "aaa", "bbb")

    assert "unavailable" in out.lower()
    assert "Per-Class Peak RAM" in out
    assert "4242" in out
    assert "Queue Wait" in out
    store.close()
