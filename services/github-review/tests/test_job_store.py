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
