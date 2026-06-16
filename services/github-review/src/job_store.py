from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    POSTED = "posted"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class ReviewJob:
    id: int
    repo: str
    project: str
    provider: str
    pr_number: int
    head_sha: str
    base_sha: str
    status: JobStatus
    attempts: int
    queued_at: float
    started_at: float | None
    finished_at: float | None
    last_error: str | None


class ReviewJobStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS review_jobs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  repo TEXT NOT NULL,
                  project TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  pr_number INTEGER NOT NULL,
                  head_sha TEXT NOT NULL,
                  base_sha TEXT NOT NULL,
                  status TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  queued_at REAL NOT NULL,
                  started_at REAL,
                  finished_at REAL,
                  last_error TEXT,
                  UNIQUE(repo, pr_number, head_sha)
                )
                """
            )

    def enqueue(
        self,
        repo: str,
        project: str,
        provider: str,
        pr_number: int,
        head_sha: str,
        base_sha: str,
    ) -> ReviewJob:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO review_jobs
                  (repo, project, provider, pr_number, head_sha, base_sha, status, queued_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (repo, project, provider, pr_number, head_sha, base_sha, JobStatus.QUEUED, now),
            )
            row = conn.execute(
                """
                SELECT * FROM review_jobs
                WHERE repo = ? AND pr_number = ? AND head_sha = ?
                """,
                (repo, pr_number, head_sha),
            ).fetchone()
        return self._from_row(row)

    def get(self, job_id: int) -> ReviewJob | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM review_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._from_row(row) if row else None

    def list_by_status(self, status: JobStatus) -> list[ReviewJob]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM review_jobs WHERE status = ? ORDER BY id",
                (status,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def mark_running(self, job_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE review_jobs
                SET status = ?, started_at = ?, attempts = attempts + 1, last_error = NULL
                WHERE id = ?
                """,
                (JobStatus.RUNNING, time.time(), job_id),
            )

    def mark_posted(self, job_id: int) -> None:
        self._finish(job_id, JobStatus.POSTED, None)

    def mark_skipped(self, job_id: int, reason: str) -> None:
        self._finish(job_id, JobStatus.SKIPPED, reason)

    def mark_failed(self, job_id: int, error: str) -> None:
        self._finish(job_id, JobStatus.FAILED, error)

    def _finish(self, job_id: int, status: JobStatus, message: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE review_jobs
                SET status = ?, finished_at = ?, last_error = ?
                WHERE id = ?
                """,
                (status, time.time(), message, job_id),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _from_row(self, row: sqlite3.Row) -> ReviewJob:
        return ReviewJob(
            id=int(row["id"]),
            repo=str(row["repo"]),
            project=str(row["project"]),
            provider=str(row["provider"]),
            pr_number=int(row["pr_number"]),
            head_sha=str(row["head_sha"]),
            base_sha=str(row["base_sha"]),
            status=JobStatus(str(row["status"])),
            attempts=int(row["attempts"]),
            queued_at=float(row["queued_at"]),
            started_at=float(row["started_at"]) if row["started_at"] is not None else None,
            finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        )
