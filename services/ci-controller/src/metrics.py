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
    peak_cpu_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class MetricsStore:
    """Durable append-only log of admission decisions and lane reaps."""

    def __init__(self, db_path: str) -> None:
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

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
    ) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, kind, job_id, repo, class, work_disk, reason, "
            "config_version, lane_id, peak_ram_mb, peak_cpu_pct) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
