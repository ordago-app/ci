from __future__ import annotations

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    job_id INTEGER NOT NULL,
    repo TEXT NOT NULL,
    class TEXT,
    work_disk TEXT,
    reason TEXT,
    config_version TEXT NOT NULL,
    lane_id TEXT,
    peak_ram_mb INTEGER,
    peak_cpu_pct REAL,
    reasons TEXT,
    job_name TEXT,
    workflow TEXT,
    host TEXT,
    conclusion TEXT,
    spawned_for_job_id INTEGER,
    attributed INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""

_ADDED_COLUMNS = {
    "reasons": "TEXT",
    "job_name": "TEXT",
    "workflow": "TEXT",
    "host": "TEXT",
    "conclusion": "TEXT",
    # job_id holds the observed running job once attributed=1; spawned_for_job_id keeps the
    # admission-time prediction so spawn-vs-run divergence stays measurable. Both are NULL on
    # every row written before attribution existed — that NULL is the marker that those rows'
    # per-job identity is a prediction, not an observation.
    "spawned_for_job_id": "INTEGER",
    "attributed": "INTEGER",
}


class MetricsStore:
    """Durable append-only log of admission decisions and lane reaps."""

    def __init__(self, db_path: str) -> None:
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive-only. Existing rows keep NULL for new columns; nothing is backfilled."""
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(events)")}
        for column, sql_type in _ADDED_COLUMNS.items():
            if column not in existing:
                self.conn.execute(f"ALTER TABLE events ADD COLUMN {column} {sql_type}")

    def record_event(
        self,
        *,
        kind: str,
        job_id: int,
        repo: str,
        ts: float,
        config_version: str,
        class_name: str | None = None,
        work_disk: str | None = None,
        reason: str | None = None,
        lane_id: str | None = None,
        peak_ram_mb: int | None = None,
        peak_cpu_pct: float | None = None,
        reasons: str | None = None,
        job_name: str | None = None,
        workflow: str | None = None,
        host: str | None = None,
        conclusion: str | None = None,
        spawned_for_job_id: int | None = None,
        attributed: int | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, kind, job_id, repo, class, work_disk, reason, "
            "config_version, lane_id, peak_ram_mb, peak_cpu_pct, reasons, job_name, "
            "workflow, host, conclusion, spawned_for_job_id, attributed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                kind,
                job_id,
                repo,
                class_name,
                work_disk,
                reason,
                config_version,
                lane_id,
                peak_ram_mb,
                peak_cpu_pct,
                reasons,
                job_name,
                workflow,
                host,
                conclusion,
                spawned_for_job_id,
                attributed,
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
