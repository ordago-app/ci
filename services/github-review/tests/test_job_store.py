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
