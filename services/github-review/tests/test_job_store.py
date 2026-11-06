import sqlite3
import time
from pathlib import Path

from src.job_store import InterruptedOutcome, JobStatus, ReviewJobStore

from tests.helpers import begin_attempt

WEEK = 7 * 24 * 3600


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
    begin_attempt(store, job.id)
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
    begin_attempt(store, under.id)  # attempts -> 1
    store.mark_failed(under.id, "boom")

    exhausted = store.enqueue("alvaro/homelab", "homelab", "codex", 3, "x", "base")
    for _ in range(3):  # attempts -> 3
        begin_attempt(store, exhausted.id)
        store.mark_failed(exhausted.id, "boom")

    retryable_ids = {job.id for job in store.list_retryable(max_attempts=3)}
    assert queued.id in retryable_ids
    assert under.id in retryable_ids
    assert exhausted.id not in retryable_ids


def test_mark_posted_records_verdict(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "abc", "base")
    begin_attempt(store, job.id)
    store.mark_posted(job.id, verdict="APPROVE")
    updated = store.get(job.id)
    assert updated is not None
    assert updated.status == JobStatus.POSTED
    assert updated.verdict == "APPROVE"


def test_rounds_count_disagreements_and_runs_count_every_review(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    for sha, verdict in (("a", "REQUEST_CHANGES"), ("b", "APPROVE"), ("c", None)):
        job = store.enqueue("alvaro/homelab", "homelab", "codex", 7, sha, "base")
        begin_attempt(store, job.id)
        store.record_provider_run(job.id, "codex")
        store.mark_posted(job.id, verdict=verdict)
    carried = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "d", "base")
    store.mark_carried(carried.id, "b", "codex")
    # Paid twice, posted never: a run is spend, not a posting.
    failed = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "e", "base")
    for _ in range(2):
        begin_attempt(store, failed.id)
        store.record_provider_run(failed.id, "codex")
        store.mark_failed(failed.id, "post_review: 502")
    # A legacy NULL verdict reads as REQUEST_CHANGES; a carry is neither a round nor a run.
    assert store.rounds_for("alvaro/homelab", 7) == 2
    assert store.runs_for("alvaro/homelab", 7) == 5


def test_rounds_for_counts_posted_head_shas(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    for sha in ("a", "b"):
        job = store.enqueue("alvaro/homelab", "homelab", "codex", 7, sha, "base")
        begin_attempt(store, job.id)
        store.mark_posted(job.id, verdict="REQUEST_CHANGES")
    # A queued-but-not-posted head does not count.
    store.enqueue("alvaro/homelab", "homelab", "codex", 7, "c", "base")
    assert store.rounds_for("alvaro/homelab", 7) == 2


def test_find_returns_job_for_matching_triple(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "abc", "base")
    begin_attempt(store, job.id)
    store.mark_posted(job.id, verdict="APPROVE")
    assert store.find("alvaro/homelab", 7, "abc") is not None
    assert store.find("alvaro/homelab", 7, "other") is None


def test_a_failed_job_waits_out_the_retry_backoff(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "abc", "base")
    begin_attempt(store, job.id)
    store.mark_failed(job.id, "transient")
    failed_at = store.get(job.id).finished_at

    assert store.list_retryable(3, retry_backoff_seconds=900, now=failed_at + 899) == []
    assert [
        j.id for j in store.list_retryable(3, retry_backoff_seconds=900, now=failed_at + 900)
    ] == [job.id]


def test_requeue_resets_attempts_but_not_the_run_count(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "abc", "base")
    for _ in range(3):
        begin_attempt(store, job.id)
        store.record_provider_run(job.id, "codex")
        store.mark_failed(job.id, "boom")

    store.requeue(job.id)

    again = store.get(job.id)
    assert again is not None
    assert (again.status, again.attempts, again.last_error) == (JobStatus.QUEUED, 0, None)
    assert store.runs_for("alvaro/homelab", 7) == 3


def test_count_skipped_with_reason_matches_by_prefix(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    a = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "a", "base")
    b = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "b", "base")
    store.mark_skipped(a.id, "max review rounds (5) reached")
    store.mark_skipped(b.id, "head moved to c")
    assert store.count_skipped_with_reason("alvaro/homelab", 7, "max review ") == 1
    assert store.count_skipped_with_reason("alvaro/homelab", 8, "max review ") == 0


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
    assert store.runs_for("alvaro/homelab", 7) == 1, (
        "a review posted before runs were counted cost one"
    )


def test_count_stuck_counts_only_failures_with_no_retries_left(tmp_path: Path) -> None:
    """The exact complement of list_retryable: a job is either going to be picked up
    again, or it needs a human. Counting a retryable failure as stuck would flag
    something that is already self-healing."""
    store = make_store(tmp_path)
    store.enqueue("alvaro/homelab", "homelab", "codex", 1, "q", "base")  # queued

    under = store.enqueue("alvaro/homelab", "homelab", "codex", 2, "u", "base")
    begin_attempt(store, under.id)  # attempts -> 1
    store.mark_failed(under.id, "boom")

    exhausted = store.enqueue("alvaro/homelab", "homelab", "codex", 3, "x", "base")
    for _ in range(3):  # attempts -> 3
        begin_attempt(store, exhausted.id)
        store.mark_failed(exhausted.id, "boom")

    assert store.count_stuck(max_attempts=3, window_seconds=WEEK, now=time.time()) == 1
    retryable = {job.id for job in store.list_retryable(max_attempts=3)}
    assert exhausted.id not in retryable, "stuck and retryable must not overlap"


def test_count_stuck_is_zero_on_a_store_that_has_never_failed(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.enqueue("alvaro/homelab", "homelab", "codex", 1, "q", "base")
    assert store.count_stuck(max_attempts=3, window_seconds=WEEK, now=time.time()) == 0


def test_count_stuck_falls_back_to_zero_while_the_lifetime_total_does_not(
    tmp_path: Path,
) -> None:
    """Why the badge reads this and not counts["failed"]: once a failure is retried and
    succeeds, nothing needs attention any more — but the lifetime total still says 1."""
    store = make_store(tmp_path)
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 1, "q", "base")
    begin_attempt(store, job.id)
    store.mark_failed(job.id, "transient")
    begin_attempt(store, job.id)
    store.mark_posted(job.id)

    assert store.count_stuck(max_attempts=3, window_seconds=WEEK, now=time.time()) == 0
    assert store.counts_by_status()["failed"] == 0  # it moved to posted


def _fail_hard(store: ReviewJobStore, pr: int, max_attempts: int = 3):
    """A job driven all the way to terminal failure — attempts exhausted."""
    job = store.enqueue("alvaro/homelab", "homelab", "codex", pr, f"sha{pr}", "base")
    for _ in range(max_attempts):
        begin_attempt(store, job.id)
        store.mark_failed(job.id, "boom")
    return job


def _backdate(store: ReviewJobStore, job_id: int, seconds_ago: float) -> None:
    with sqlite3.connect(store._path) as conn:
        conn.execute(
            "UPDATE review_jobs SET finished_at = ?, queued_at = ? WHERE id = ?",
            (time.time() - seconds_ago, time.time() - seconds_ago, job_id),
        )


def test_a_failure_older_than_the_window_is_not_stuck(tmp_path: Path) -> None:
    """The bug this window exists for. Nothing in this service ever clears a terminal
    failure — no purge, no acknowledge, no expiry — so without a bound the count is just
    as monotonic as the lifetime total it replaced. Production had 15 such rows, every
    one from a five-day stretch two months earlier, keeping the badge permanently lit.
    """
    store = make_store(tmp_path)
    old = _fail_hard(store, pr=1)
    _backdate(store, old.id, seconds_ago=50 * 24 * 3600)  # the real shape: ~50 days

    assert store.count_stuck(max_attempts=3, window_seconds=WEEK, now=time.time()) == 0
    # ...while the figure it replaced still reports it, which is exactly the problem.
    assert store.counts_by_status()["failed"] == 1


def test_a_failure_inside_the_window_is_still_stuck(tmp_path: Path) -> None:
    """The window must not silence a live problem — that would be the worse failure."""
    store = make_store(tmp_path)
    recent = _fail_hard(store, pr=2)
    _backdate(store, recent.id, seconds_ago=2 * 24 * 3600)  # 2 days: inside a 7-day window

    assert store.count_stuck(max_attempts=3, window_seconds=WEEK, now=time.time()) == 1


def test_the_window_is_measured_from_when_the_job_failed_not_when_it_was_queued(
    tmp_path: Path,
) -> None:
    """A job queued long ago that only failed today is a problem today. Keying on
    queued_at would age it out while it was still fresh."""
    store = make_store(tmp_path)
    job = _fail_hard(store, pr=3)
    with sqlite3.connect(store._path) as conn:
        conn.execute(
            "UPDATE review_jobs SET queued_at = ?, finished_at = ? WHERE id = ?",
            (time.time() - 40 * 24 * 3600, time.time() - 3600, job.id),
        )

    assert store.count_stuck(max_attempts=3, window_seconds=WEEK, now=time.time()) == 1


def test_a_row_with_no_finish_time_falls_back_to_queued_at(tmp_path: Path) -> None:
    """Rows written before finished_at was populated must not vanish from the count
    (NULL >= x is NULL in SQL, which would silently drop them)."""
    store = make_store(tmp_path)
    job = _fail_hard(store, pr=4)
    with sqlite3.connect(store._path) as conn:
        conn.execute(
            "UPDATE review_jobs SET finished_at = NULL, queued_at = ? WHERE id = ?",
            (time.time() - 3600, job.id),
        )

    assert store.count_stuck(max_attempts=3, window_seconds=WEEK, now=time.time()) == 1


def test_a_job_is_claimed_by_exactly_one_runner(tmp_path: Path) -> None:
    import threading

    store = make_store(tmp_path)
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "abc", "base")
    wins: list[bool] = []
    barrier = threading.Barrier(8)

    def race() -> None:
        barrier.wait()
        wins.append(store.claim(job.id, max_attempts=3))

    threads = [threading.Thread(target=race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert wins.count(True) == 1
    claimed = store.get(job.id)
    assert claimed is not None
    assert (claimed.status, claimed.attempts) == (JobStatus.RUNNING, 1)


def test_only_queued_and_failed_jobs_can_be_claimed(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "abc", "base")
    assert store.claim(job.id, max_attempts=3)
    assert not store.claim(job.id, max_attempts=3), "a running job is someone else's"
    store.mark_failed(job.id, "boom")
    assert store.claim(job.id, max_attempts=3), "a failed job is retryable"
    store.mark_posted(job.id)
    assert not store.claim(job.id, max_attempts=3)


def test_a_stale_snapshot_cannot_claim_a_job_that_failed_meanwhile(tmp_path: Path) -> None:
    """Two runners list the queue at once; one claims and fails the job before
    the other's claim lands. That claim must see the job as it is now: out of
    attempts, inside its backoff, or failed after the drain's cutoff."""
    store = make_store(tmp_path)
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 7, "abc", "base")
    as_of = time.time()
    snapshot = store.list_retryable(1, now=as_of)
    assert [j.id for j in snapshot] == [job.id]
    assert store.claim(job.id, max_attempts=1, now=as_of)
    store.mark_failed(job.id, "boom")

    assert not store.claim(job.id, max_attempts=1), "out of attempts"
    assert not store.claim(job.id, max_attempts=3, retry_backoff_seconds=900), "in backoff"
    assert not store.claim(job.id, max_attempts=3, now=as_of), "failed after the cutoff"
    assert store.claim(job.id, max_attempts=3)


def test_the_oldest_pr_is_reviewed_first(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    old_first = store.enqueue("alvaro/homelab", "homelab", "codex", 1, "a1", "base")
    begin_attempt(store, old_first.id)
    store.mark_posted(old_first.id, verdict="REQUEST_CHANGES")
    young = store.enqueue("alvaro/homelab", "homelab", "codex", 2, "b1", "base")
    old_again = store.enqueue("alvaro/homelab", "homelab", "codex", 1, "a2", "base")
    # PR 1 was first seen before PR 2, so its newer head goes first.
    assert [j.id for j in store.list_retryable(3)] == [old_again.id, young.id]


def test_a_pr_with_a_running_review_offers_no_other_head(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    running = store.enqueue("alvaro/homelab", "homelab", "codex", 1, "a1", "base")
    begin_attempt(store, running.id)
    store.enqueue("alvaro/homelab", "homelab", "codex", 1, "a2", "base")
    other = store.enqueue("alvaro/homelab", "homelab", "codex", 2, "b1", "base")
    assert [j.id for j in store.list_retryable(3)] == [other.id]


def test_recover_interrupted_returns_running_rows_without_charging_an_attempt(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    orphan = store.enqueue("alvaro/homelab", "homelab", "codex", 1, "a", "base")
    begin_attempt(store, orphan.id)
    store.mark_failed(orphan.id, "transient")
    begin_attempt(store, orphan.id)  # attempts -> 2, then the process dies
    posted = store.enqueue("alvaro/homelab", "homelab", "codex", 2, "b", "base")
    begin_attempt(store, posted.id)
    store.mark_posted(posted.id)

    assert store.recover_interrupted(orphan.id, 2, "interrupted by a restart") == (
        InterruptedOutcome.REQUEUED
    )
    assert store.recover_interrupted(posted.id, 2, "interrupted by a restart") is None

    requeued = store.get(orphan.id)
    assert requeued is not None
    assert (requeued.status, requeued.attempts, requeued.started_at, requeued.restart_refunds) == (
        JobStatus.QUEUED,
        1,
        None,
        1,
    )
    assert requeued.last_error is not None and "restart" in requeued.last_error
    assert store.get(posted.id).status == JobStatus.POSTED  # type: ignore[union-attr]
    assert store.recover_interrupted(orphan.id, 2, "x") is None, "only a running job"


def test_a_claim_loses_to_another_head_of_the_same_pr_already_running(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.enqueue("alvaro/homelab", "homelab", "codex", 1, "a1", "base")
    second = store.enqueue("alvaro/homelab", "homelab", "codex", 1, "a2", "base")
    assert store.claim(first.id, max_attempts=3)
    assert not store.claim(second.id, max_attempts=3)
    store.mark_posted(first.id)
    assert store.claim(second.id, max_attempts=3)


def test_past_the_refund_cap_an_interruption_counts_as_a_failed_attempt(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 1, "a", "base")
    for _ in range(2):
        begin_attempt(store, job.id)
        assert store.recover_interrupted(job.id, 2, "interrupted by a restart") == (
            InterruptedOutcome.REQUEUED
        )
    begin_attempt(store, job.id)

    assert store.recover_interrupted(job.id, 2, "interrupted by a restart") == (
        InterruptedOutcome.FAILED
    )

    failed = store.get(job.id)
    assert failed is not None
    assert (failed.status, failed.attempts, failed.restart_refunds) == (JobStatus.FAILED, 1, 2)
    assert failed.finished_at is not None
    assert failed.last_error is not None and "counted as a failed attempt" in failed.last_error
    assert [j.id for j in store.list_retryable(3, retry_backoff_seconds=0.0)] == [job.id], (
        "a counted interruption retries like any failure while attempts remain"
    )


def test_a_cap_of_zero_never_refunds(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 1, "a", "base")
    begin_attempt(store, job.id)
    assert store.recover_interrupted(job.id, 0, "x") == InterruptedOutcome.FAILED


def test_an_explicit_requeue_restores_the_refunds(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 1, "a", "base")
    begin_attempt(store, job.id)
    store.recover_interrupted(job.id, 2, "x")
    store.requeue(job.id)
    assert store.get(job.id).restart_refunds == 0  # type: ignore[union-attr]


def test_list_running_since_finds_only_old_claims(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    old = store.enqueue("alvaro/homelab", "homelab", "codex", 1, "a", "base")
    fresh = store.enqueue("alvaro/homelab", "homelab", "codex", 2, "b", "base")
    store.enqueue("alvaro/homelab", "homelab", "codex", 3, "c", "base")  # queued, never running
    begin_attempt(store, old.id)
    begin_attempt(store, fresh.id)
    with sqlite3.connect(store._path) as conn:
        conn.execute("UPDATE review_jobs SET started_at = 1.0 WHERE id = ?", (old.id,))

    assert [j.id for j in store.list_running_since(time.time() - 60)] == [old.id]


def test_a_green_head_jumps_older_prs(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    oldest = store.enqueue("alvaro/homelab", "homelab", "codex", 1, "a1", "base")
    middle = store.enqueue("alvaro/homelab", "homelab", "codex", 2, "b1", "base")
    green_young = store.enqueue("alvaro/homelab", "homelab", "codex", 3, "c1", "base")
    green_old = store.enqueue("alvaro/homelab", "homelab", "codex", 4, "d1", "base")
    store.set_ci_green(green_young.id, True)
    store.set_ci_green(green_old.id, True)
    with sqlite3.connect(store._path) as conn:
        conn.execute("UPDATE review_jobs SET queued_at = 0.5 WHERE id = ?", (green_old.id,))

    # Green first, then PR age within each group.
    assert [j.id for j in store.list_retryable(3)] == [
        green_old.id,
        green_young.id,
        oldest.id,
        middle.id,
    ]
    assert store.get(green_young.id).ci_green  # type: ignore[union-attr]


def test_init_adds_refund_and_ci_columns_to_the_live_schema_in_place(tmp_path: Path) -> None:
    """The schema prod ran before this change, with a job mid-review: init must
    add the columns without losing the row, and the row must recover normally."""
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE review_jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          repo TEXT NOT NULL, project TEXT NOT NULL, provider TEXT NOT NULL,
          pr_number INTEGER NOT NULL, head_sha TEXT NOT NULL, base_sha TEXT NOT NULL,
          status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          queued_at REAL NOT NULL, started_at REAL, finished_at REAL, last_error TEXT,
          verdict TEXT, carried_from TEXT, provider_runs INTEGER NOT NULL DEFAULT 0,
          UNIQUE(repo, pr_number, head_sha)
        )
        """
    )
    conn.execute(
        "INSERT INTO review_jobs "
        "(repo, project, provider, pr_number, head_sha, base_sha, status, attempts, "
        "queued_at, started_at) "
        "VALUES ('alvaro/homelab','homelab','codex',7,'abc','base','running',1,1.0,2.0)"
    )
    conn.commit()
    conn.close()

    store = ReviewJobStore(db)
    store.init()
    store.init()  # idempotent on every later start

    job = store.get(1)
    assert job is not None
    assert (job.status, job.restart_refunds, job.ci_green) == (JobStatus.RUNNING, 0, False)
    assert store.recover_interrupted(1, 2, "interrupted by a restart") == (
        InterruptedOutcome.REQUEUED
    )


def test_spend_is_kept_per_account_and_unpriced_runs_are_counted_apart(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    job = store.enqueue("o/r", "r", "claude-a", 1, "a" * 40, "b" * 40)

    # A failover: one account spent and gave out, the next one reviewed.
    store.record_provider_run(job.id, "claude-a", 0.5)
    store.record_provider_run(job.id, "claude-b", 1.25)
    store.record_provider_run(job.id, "claude-b", 0.25)
    store.record_provider_run(job.id, "codex", None)

    assert store.spend_by_account(0) == [
        {"account": "claude-b", "runs": 2, "priced_runs": 2, "cost_usd": 1.5},
        {"account": "claude-a", "runs": 1, "priced_runs": 1, "cost_usd": 0.5},
        {"account": "codex", "runs": 1, "priced_runs": 0, "cost_usd": 0.0},
    ]
    assert store.spend_by_account(time.time() + 60) == []
    assert store.runs_for("o/r", 1) == 4


def test_the_run_ledger_is_added_to_a_db_that_predates_it(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    ReviewJobStore(path).init()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE review_runs")
    store = ReviewJobStore(path)
    store.init()
    job = store.enqueue("o/r", "r", "codex", 1, "a" * 40, "b" * 40)

    store.record_provider_run(job.id, "codex", 0.2)

    assert store.spend_by_account(0)[0]["cost_usd"] == 0.2
