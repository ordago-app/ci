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
    verdict: str | None = None
    # The head whose APPROVE this row re-posted without a review run; see
    # ReviewWorker._carry_approval. None for every review that read the diff.
    carried_from: str | None = None
    # Attempts refunded because the process holding the job died or lost it; see
    # ReviewJobStore.recover_interrupted.
    restart_refunds: int = 0
    # Every check run on the head had passed at the last poll that could read them.
    ci_green: bool = False
    # What the provider wrote, before the worker's disclosures; the next round
    # checks its findings. None on carried rows and rows posted before it was kept.
    review_body: str | None = None


class InterruptedOutcome(StrEnum):
    REQUEUED = "requeued"
    FAILED = "failed"


# A job a runner may take: queued, or failed with attempts left and its retry
# backoff waited out, and no review of the same PR running. Shared by the queue
# listing and the claim, so the two can never disagree.
_CLAIMABLE = """
    (status = ?
     OR (status = ? AND attempts < ? AND COALESCE(finished_at, queued_at) <= ?))
    AND NOT EXISTS (
      SELECT 1 FROM review_jobs r
      WHERE r.repo = review_jobs.repo AND r.pr_number = review_jobs.pr_number
        AND r.status = ?)
"""


def _claimable_params(
    max_attempts: int, retry_backoff_seconds: float, now: float | None
) -> tuple[object, ...]:
    failed_before = (time.time() if now is None else now) - retry_backoff_seconds
    return (JobStatus.QUEUED, JobStatus.FAILED, max_attempts, failed_before, JobStatus.RUNNING)


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
                  verdict TEXT,
                  carried_from TEXT,
                  provider_runs INTEGER NOT NULL DEFAULT 0,
                  restart_refunds INTEGER NOT NULL DEFAULT 0,
                  ci_green INTEGER NOT NULL DEFAULT 0,
                  review_body TEXT,
                  UNIQUE(repo, pr_number, head_sha)
                )
                """
            )
            cols = {row[1] for row in conn.execute("PRAGMA table_info(review_jobs)")}
            if "verdict" not in cols:
                conn.execute("ALTER TABLE review_jobs ADD COLUMN verdict TEXT")
            if "carried_from" not in cols:
                conn.execute("ALTER TABLE review_jobs ADD COLUMN carried_from TEXT")
            if "provider_runs" not in cols:
                conn.execute(
                    "ALTER TABLE review_jobs ADD COLUMN provider_runs INTEGER NOT NULL DEFAULT 0"
                )
                # Older rows recorded no invocations; a review that posted cost at least one.
                conn.execute(
                    "UPDATE review_jobs SET provider_runs = 1 "
                    "WHERE status = ? AND carried_from IS NULL",
                    (JobStatus.POSTED,),
                )
            # In place on the live DB: SQLite fills a NOT NULL DEFAULT column on
            # every existing row, so no row is rewritten and none is lost.
            if "restart_refunds" not in cols:
                conn.execute(
                    "ALTER TABLE review_jobs ADD COLUMN restart_refunds INTEGER NOT NULL DEFAULT 0"
                )
            if "ci_green" not in cols:
                conn.execute(
                    "ALTER TABLE review_jobs ADD COLUMN ci_green INTEGER NOT NULL DEFAULT 0"
                )
            if "review_body" not in cols:
                conn.execute("ALTER TABLE review_jobs ADD COLUMN review_body TEXT")

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

    def find(self, repo: str, pr_number: int, head_sha: str) -> ReviewJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM review_jobs WHERE repo = ? AND pr_number = ? AND head_sha = ?",
                (repo, pr_number, head_sha),
            ).fetchone()
        return self._from_row(row) if row else None

    def latest_for_heads(self, repo: str, head_shas: list[str]) -> dict[str, ReviewJob]:
        """The newest row per head SHA in `repo`, for the heads that have one.

        Keyed by SHA alone, not (PR, SHA): one commit heading two PRs is one
        piece of CI, so the most recent review of it is the one that counts."""
        if not head_shas:
            return {}
        placeholders = ",".join("?" * len(head_shas))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM review_jobs WHERE repo = ? AND head_sha IN ({placeholders}) "
                "ORDER BY id",
                (repo, *head_shas),
            ).fetchall()
        return {str(row["head_sha"]): self._from_row(row) for row in rows}

    def requeue(self, job_id: int) -> None:
        """Give a job that ran out of attempts a fresh set, restart refunds included.
        Spend stays bounded: `provider_runs` is not reset, so the run cap still
        counts every paid run."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE review_jobs SET status = ?, attempts = 0, restart_refunds = 0, "
                "last_error = NULL, finished_at = NULL WHERE id = ?",
                (JobStatus.QUEUED, job_id),
            )

    def count_skipped_with_reason(self, repo: str, pr_number: int, reason_prefix: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM review_jobs WHERE repo = ? AND pr_number = ? "
                "AND status = ? AND last_error LIKE ? || '%'",
                (repo, pr_number, JobStatus.SKIPPED, reason_prefix),
            ).fetchone()
        return int(row["n"])

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

    def counts_by_status(self) -> dict[str, int]:
        """Job totals keyed by status. Every JobStatus is present, zero-filled, so a
        consumer can render a full row without special-casing the empty store."""
        counts = {s.value: 0 for s in JobStatus}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM review_jobs GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[str(row["status"])] = int(row["n"])
        return counts

    def count_stuck(self, max_attempts: int, window_seconds: float, now: float) -> int:
        """Failed jobs with no retries left that failed RECENTLY — the ones actionable now.

        Both halves of that predicate are load-bearing, and the first alone is not enough.

        Out-of-retries excludes what is still self-healing: a failed job with attempts
        left gets claimed again from `list_retryable`, once its retry backoff
        has passed. A failure inside its backoff is in neither set: still self-healing,
        not stuck.

        The window is what makes the number able to fall back to zero. Nothing in this
        service ever clears a terminal failure — there is no purge, no acknowledge, no
        expiry — so "failed and out of retries" is just as monotonic as the lifetime
        `counts_by_status()["failed"]` it was meant to replace. Shipped without the
        window it reported 15 on this box, every row from a five-day stretch two months
        earlier, and the badge it feeds stayed lit exactly as before.

        Recency is measured from `finished_at` (when it actually failed), falling back to
        `queued_at` for rows written before that column was populated. A job whose failure
        predates the window is history, not a task: the PR it was reviewing has long since
        merged or closed, and there is nothing left to do about it today.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM review_jobs
                WHERE status = ? AND attempts >= ?
                  AND COALESCE(finished_at, queued_at) >= ?
                """,
                (JobStatus.FAILED, max_attempts, now - window_seconds),
            ).fetchone()
        return int(row["n"])

    def list_active(self) -> list[ReviewJob]:
        """Jobs still in flight (queued or running), newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM review_jobs WHERE status IN (?, ?) ORDER BY queued_at DESC, id DESC",
                (JobStatus.QUEUED, JobStatus.RUNNING),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_retryable(
        self, max_attempts: int, retry_backoff_seconds: float = 0.0, now: float | None = None
    ) -> list[ReviewJob]:
        """Jobs a runner may claim next, highest priority first.

        Queued jobs plus failed jobs that haven't exhausted their attempts and
        have waited out the backoff. The backoff is explicit, not "one pass":
        a runner drains the queue back to back, so without it a provider outage
        would spend every attempt in seconds.

        A PR with a review already running is left out: its other heads are
        either superseded (the run path skips them as `head moved`) or would
        race it on the round and run caps.

        Priority: heads whose CI is all green first (the review is their last
        blocker), then the PR's age, oldest first, measured from the first job
        this store ever queued for it: the PR that has waited longest is the
        closest to merging. `ci_green` is written by the poll (see
        `set_ci_green`); while check runs are unreadable every row is 0 and the
        order is PR age alone.
        """
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM review_jobs
                WHERE {_CLAIMABLE}
                ORDER BY ci_green DESC,
                         (SELECT MIN(p.queued_at) FROM review_jobs p
                          WHERE p.repo = review_jobs.repo AND p.pr_number = review_jobs.pr_number),
                         id
                """,
                _claimable_params(max_attempts, retry_backoff_seconds, now),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def claim(
        self,
        job_id: int,
        max_attempts: int,
        retry_backoff_seconds: float = 0.0,
        now: float | None = None,
    ) -> bool:
        """Move a job `list_retryable` would offer to running, spending one attempt.

        False when it is no longer claimable: another runner took it, failed it
        inside its backoff or out of attempts, or holds another head of the same
        PR. The check and the write are one statement, so a stale snapshot of the
        queue can never win a job twice."""
        with self._connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE review_jobs
                SET status = ?, started_at = ?, attempts = attempts + 1, last_error = NULL
                WHERE id = ? AND {_CLAIMABLE}
                """,
                (
                    JobStatus.RUNNING,
                    time.time(),
                    job_id,
                    *_claimable_params(max_attempts, retry_backoff_seconds, now),
                ),
            )
            return cur.rowcount == 1

    def recover_interrupted(
        self, job_id: int, max_refunds: int, reason: str
    ) -> InterruptedOutcome | None:
        """Take a `running` job no runner of this process owns out of `running`.

        Its first `max_refunds` interruptions go back to the queue with the
        attempt refunded: a restart says nothing about the review. Past that the
        review itself is the likely cause (it crashes or hangs the service every
        time), so the attempt counts and the job is failed like any other,
        retried after the backoff while attempts remain. None when the job is no
        longer running. A job whose PR has closed meanwhile is skipped by the run
        path, never reviewed."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE review_jobs
                SET status = ?, attempts = MAX(attempts - 1, 0),
                    restart_refunds = restart_refunds + 1, started_at = NULL,
                    last_error = ?
                WHERE id = ? AND status = ? AND restart_refunds < ?
                """,
                (JobStatus.QUEUED, f"{reason}; requeued", job_id, JobStatus.RUNNING, max_refunds),
            )
            if cur.rowcount == 1:
                return InterruptedOutcome.REQUEUED
            cur = conn.execute(
                "UPDATE review_jobs SET status = ?, finished_at = ?, last_error = ? "
                "WHERE id = ? AND status = ?",
                (
                    JobStatus.FAILED,
                    time.time(),
                    f"{reason} again after {max_refunds} refunded; counted as a failed attempt",
                    job_id,
                    JobStatus.RUNNING,
                ),
            )
            return InterruptedOutcome.FAILED if cur.rowcount == 1 else None

    def list_running_since(self, started_before: float) -> list[ReviewJob]:
        """Running jobs claimed before `started_before`, oldest claim first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM review_jobs WHERE status = ? AND started_at < ? "
                "ORDER BY started_at, id",
                (JobStatus.RUNNING, started_before),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def set_ci_green(self, job_id: int, green: bool) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE review_jobs SET ci_green = ? WHERE id = ?", (int(green), job_id))

    def mark_posted(
        self,
        job_id: int,
        verdict: str | None = None,
        provider: str | None = None,
        review_body: str | None = None,
    ) -> None:
        """`provider` is the one that actually produced the review.

        It overwrites what `enqueue` guessed, so the row answers "who reviewed
        this?" rather than "who were we going to ask?" — the only durable record
        that a failover happened.
        """
        with self._connect() as conn:
            if provider is None:
                conn.execute(
                    "UPDATE review_jobs SET status = ?, finished_at = ?, last_error = NULL, "
                    "verdict = ?, review_body = ? WHERE id = ?",
                    (JobStatus.POSTED, time.time(), verdict, review_body, job_id),
                )
            else:
                conn.execute(
                    "UPDATE review_jobs SET status = ?, finished_at = ?, last_error = NULL, "
                    "verdict = ?, provider = ?, review_body = ? WHERE id = ?",
                    (JobStatus.POSTED, time.time(), verdict, provider, review_body, job_id),
                )

    def mark_carried(self, job_id: int, source_head_sha: str, provider: str) -> None:
        """POSTED as an APPROVE that no review run produced — neither a round nor a run.

        `provider` is the one behind the approval being carried, so "who reviewed
        this?" still has an answer on a carried row."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE review_jobs SET status = ?, finished_at = ?, last_error = NULL, "
                "verdict = ?, carried_from = ?, provider = ? WHERE id = ?",
                (JobStatus.POSTED, time.time(), "APPROVE", source_head_sha, provider, job_id),
            )

    def latest_posted_before(self, repo: str, pr_number: int, head_sha: str) -> ReviewJob | None:
        """The most recent posted review of this PR at any OTHER head."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM review_jobs WHERE repo = ? AND pr_number = ? AND head_sha != ? "
                "AND status = ? ORDER BY finished_at DESC, id DESC LIMIT 1",
                (repo, pr_number, head_sha, JobStatus.POSTED),
            ).fetchone()
        return self._from_row(row) if row else None

    def mark_skipped(self, job_id: int, reason: str) -> None:
        self._finish(job_id, JobStatus.SKIPPED, reason)

    def mark_failed(self, job_id: int, error: str) -> None:
        self._finish(job_id, JobStatus.FAILED, error)

    def rounds_for(self, repo: str, pr_number: int) -> int:
        """Review rounds that ended in disagreement — the conversation the cap bounds.

        An APPROVE is not a round: it closes the conversation, and the review a
        later CI-fix push buys is a fresh look, not a continued argument. Counting
        approvals spent the budget on PRs the reviewer never objected to. A legacy
        row with no verdict counts, matching its REQUEST_CHANGES reading elsewhere.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT head_sha) FROM review_jobs "
                "WHERE repo = ? AND pr_number = ? AND status = ? AND carried_from IS NULL "
                "AND (verdict IS NULL OR verdict != 'APPROVE')",
                (repo, pr_number, JobStatus.POSTED),
            ).fetchone()
        return int(row[0])

    def record_provider_run(self, job_id: int) -> None:
        """A provider read the diff for this job — paid, whether or not a review posted."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE review_jobs SET provider_runs = provider_runs + 1 WHERE id = ?",
                (job_id,),
            )

    def runs_for(self, repo: str, pr_number: int) -> int:
        """Provider invocations on this PR, whatever came of them — the spend the run cap bounds.

        A run that timed out, returned garbage, or reviewed a head whose post then
        failed still cost money, and retries repeat it. A carried approval ran nothing.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(provider_runs), 0) FROM review_jobs "
                "WHERE repo = ? AND pr_number = ?",
                (repo, pr_number),
            ).fetchone()
        return int(row[0])

    def consecutive_failures(self, repo: str) -> int:
        """Failed attempts for `repo` since it last posted a review.

        One failure is noise — a force-push mid-review, a flaky container. A repo
        that has never recovered is a broken deployment, and only the count tells
        them apart. github-review failed every ordago-apps review for seven weeks
        without that distinction ever being drawn anywhere.

        Counts `attempts`, not rows: one job retried three times and three jobs
        failing once each are both "three failures in a row" to an operator, and
        the retry case is the one a row count would hide.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(attempts), 0) FROM review_jobs "
                "WHERE repo = ? AND status = ? AND id > "
                "COALESCE((SELECT MAX(id) FROM review_jobs WHERE repo = ? AND status = ?), 0)",
                (repo, JobStatus.FAILED, repo, JobStatus.POSTED),
            ).fetchone()
        return int(row[0])

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
            verdict=str(row["verdict"]) if row["verdict"] is not None else None,
            carried_from=str(row["carried_from"]) if row["carried_from"] is not None else None,
            restart_refunds=int(row["restart_refunds"]),
            ci_green=bool(row["ci_green"]),
            review_body=str(row["review_body"]) if row["review_body"] is not None else None,
        )
