import sqlite3

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


def test_new_columns_round_trip(tmp_path) -> None:
    store = MetricsStore(str(tmp_path / "m.db"))
    store.record_event(
        kind="defer",
        job_id=9,
        repo="o/r",
        ts=1.0,
        config_version="v",
        reason="lane_ceiling",
        reasons="lane_ceiling,budget_full",
        job_name="build-android",
        workflow="ci.yml",
        host="powerserver",
    )
    row = store.conn.execute(
        "SELECT reason, reasons, job_name, workflow, host FROM events WHERE job_id=9"
    ).fetchone()
    assert row == (
        "lane_ceiling",
        "lane_ceiling,budget_full",
        "build-android",
        "ci.yml",
        "powerserver",
    )
    store.close()


def test_migrates_a_preexisting_db_without_losing_rows(tmp_path) -> None:
    """The live DB has 112k rows written before these columns existed."""
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
    old.execute(
        "INSERT INTO events (ts, kind, job_id, repo, reason, config_version) "
        "VALUES (1.0, 'defer', 1, 'o/r', 'budget_full', 'old')"
    )
    old.commit()
    old.close()

    store = MetricsStore(str(db))

    assert store.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert store.conn.execute("SELECT reasons FROM events WHERE job_id=1").fetchone()[0] is None
    store.record_event(
        kind="defer", job_id=2, repo="o/r", ts=2.0, config_version="new", reasons="budget_full"
    )
    assert store.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2
    store.close()
