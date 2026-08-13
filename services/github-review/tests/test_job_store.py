import sqlite3
from pathlib import Path

from src.job_store import JobStatus, ReviewJobStore


def make_store(tmp_path: Path) -> ReviewJobStore:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    return store


def test_enqueue_dedupes_repo_pr_head_sha(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.enqueue(
        repo="alvaro/homelab",
        project="homelab",
        provider="codex",
        pr_number=7,
        head_sha="abc123",
        base_sha="base123",
    )
    second = store.enqueue(
        repo="alvaro/homelab",
        project="homelab",
        provider="codex",
        pr_number=7,
        head_sha="abc123",
        base_sha="base123",
    )
    assert first.id == second.id
    assert store.list_by_status(JobStatus.QUEUED) == [first]


def test_new_head_sha_creates_new_job(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "abc123", "base123")
    second = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "def456", "base123")
    assert first.id != second.id
    assert [job.head_sha for job in store.list_by_status(JobStatus.QUEUED)] == ["abc123", "def456"]


def test_transitions_record_failure_message(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "abc123", "base123")
    store.mark_running(job.id)
    store.mark_failed(job.id, "provider timed out")
    updated = store.get(job.id)
    assert updated is not None
    assert updated.status == JobStatus.FAILED
    assert updated.attempts == 1
    assert updated.last_error == "provider timed out"


def test_list_retryable_includes_queued_and_failed_under_limit(tmp_path: Path) -> None:
    # Transient failures should self-heal: failed jobs under the attempt limit
    # are retried alongside fresh queued ones; exhausted ones are terminal.
    store = make_store(tmp_path)
    queued = store.enqueue("alvaro/homelab", "homelab", "codex", 1, "q", "base")

    under = store.enqueue("alvaro/homelab", "homelab", "codex", 2, "u", "base")
    store.mark_running(under.id)  # attempts -> 1
    store.mark_failed(under.id, "boom")

    exhausted = store.enqueue("alvaro/homelab", "homelab", "codex", 3, "x", "base")
    for _ in range(3):  # attempts -> 3
        store.mark_running(exhausted.id)
        store.mark_failed(exhausted.id, "boom")

    retryable_ids = {job.id for job in store.list_retryable(max_attempts=3)}
    assert queued.id in retryable_ids
    assert under.id in retryable_ids
    assert exhausted.id not in retryable_ids


def test_mark_posted_records_verdict(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "abc", "base")
    store.mark_running(job.id)
    store.mark_posted(job.id, verdict="APPROVE")
    updated = store.get(job.id)
    assert updated is not None
    assert updated.status == JobStatus.POSTED
    assert updated.verdict == "APPROVE"


def test_rounds_for_counts_posted_head_shas(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    for sha in ("a", "b"):
        job = store.enqueue("alvaro/homelab", "homelab", "codex", 7, sha, "base")
        store.mark_running(job.id)
        store.mark_posted(job.id, verdict="REQUEST_CHANGES")
    # A queued-but-not-posted head does not count.
    store.enqueue("alvaro/homelab", "homelab", "codex", 7, "c", "base")
    assert store.rounds_for("alvaro/homelab", 7) == 2


def test_get_posted_returns_job_for_matching_triple(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "abc", "base")
    store.mark_running(job.id)
    store.mark_posted(job.id, verdict="APPROVE")
    assert store.get_posted("alvaro/homelab", 7, "abc") is not None
    assert store.get_posted("alvaro/homelab", 7, "other") is None


def test_init_migrates_legacy_db_without_verdict_column(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    # Legacy schema: same as current minus the verdict column.
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE review_jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          repo TEXT NOT NULL, project TEXT NOT NULL, provider TEXT NOT NULL,
          pr_number INTEGER NOT NULL, head_sha TEXT NOT NULL, base_sha TEXT NOT NULL,
          status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          queued_at REAL NOT NULL, started_at REAL, finished_at REAL, last_error TEXT,
          UNIQUE(repo, pr_number, head_sha)
        )
        """
    )
    conn.execute(
        "INSERT INTO review_jobs "
        "(repo, project, provider, pr_number, head_sha, base_sha, status, queued_at) "
        "VALUES ('alvaro/homelab','homelab','codex',7,'abc','base','posted',1.0)"
    )
    conn.commit()
    conn.close()

    store = ReviewJobStore(db)
    store.init()  # must add the missing column, not raise
    job = store.get(1)
    assert job is not None
    assert job.verdict is None  # legacy row reads back cleanly
    store.mark_posted(1, verdict="APPROVE")
    assert store.get(1).verdict == "APPROVE"


def test_count_stuck_counts_only_failures_with_no_retries_left(tmp_path: Path) -> None:
    """The exact complement of list_retryable: a job is either going to be picked up
    again, or it needs a human. Counting a retryable failure as stuck would flag
    something that is already self-healing."""
    store = make_store(tmp_path)
    store.enqueue("alvaro/homelab", "homelab", "codex", 1, "q", "base")  # queued

    under = store.enqueue("alvaro/homelab", "homelab", "codex", 2, "u", "base")
    store.mark_running(under.id)  # attempts -> 1
    store.mark_failed(under.id, "boom")

    exhausted = store.enqueue("alvaro/homelab", "homelab", "codex", 3, "x", "base")
    for _ in range(3):  # attempts -> 3
        store.mark_running(exhausted.id)
        store.mark_failed(exhausted.id, "boom")

    assert store.count_stuck(max_attempts=3) == 1
    retryable = {job.id for job in store.list_retryable(max_attempts=3)}
    assert exhausted.id not in retryable, "stuck and retryable must not overlap"


def test_count_stuck_is_zero_on_a_store_that_has_never_failed(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.enqueue("alvaro/homelab", "homelab", "codex", 1, "q", "base")
    assert store.count_stuck(max_attempts=3) == 0


def test_count_stuck_falls_back_to_zero_while_the_lifetime_total_does_not(
    tmp_path: Path,
) -> None:
    """Why the badge reads this and not counts["failed"]: once a failure is retried and
    succeeds, nothing needs attention any more — but the lifetime total still says 1."""
    store = make_store(tmp_path)
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 1, "q", "base")
    store.mark_running(job.id)
    store.mark_failed(job.id, "transient")
    store.mark_running(job.id)
    store.mark_posted(job.id)

    assert store.count_stuck(max_attempts=3) == 0
    assert store.counts_by_status()["failed"] == 0  # it moved to posted
