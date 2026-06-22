from src.metrics import MetricsStore


def test_records_and_reads_back_events(tmp_path) -> None:
    store = MetricsStore(str(tmp_path / "m.db"))
    store.record_event(
        kind="admit",
        job_id=42,
        repo="o/r",
        ts=100.0,
        config_version="abc12345",
        class_name="node",
        work_disk="hdd",
        lane_id="lane-42",
    )
    store.record_event(
        kind="reap",
        job_id=42,
        repo="o/r",
        ts=160.0,
        config_version="abc12345",
        class_name="node",
        lane_id="lane-42",
        peak_ram_mb=2900,
        peak_cpu_pct=140.5,
    )
    rows = store.conn.execute(
        "SELECT kind, job_id, class, work_disk, peak_ram_mb FROM events ORDER BY ts"
    ).fetchall()
    assert rows[0] == ("admit", 42, "node", "hdd", None)
    assert rows[1] == ("reap", 42, "node", None, 2900)
    store.close()


def test_defer_event_stores_reason(tmp_path) -> None:
    store = MetricsStore(str(tmp_path / "m.db"))
    store.record_event(
        kind="defer",
        job_id=7,
        repo="o/r",
        ts=10.0,
        config_version="v",
        class_name="node",
        work_disk="hdd",
        reason="budget_full",
    )
    (reason,) = store.conn.execute("SELECT reason FROM events WHERE job_id=7").fetchone()
    assert reason == "budget_full"
    store.close()
