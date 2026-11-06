"""Up to `concurrency` reviews at once, without polling ever waiting on one."""

import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from src.github_client import PullRequest
from src.job_store import JobStatus, ReviewJobStore
from src.provider import ProviderUnavailable, ReviewResult, SessionRef
from src.worker import NO_REVIEW_MARKER, ReviewWorker

from tests.helpers import begin_attempt
from tests.test_worker_flow import FakeGitHub, FakeWorktrees, config

REPO = "alvaro-francisco-gil/homelab"


class ManyPRs(FakeGitHub):
    def __init__(self, count: int, state: str = "open") -> None:
        super().__init__()
        self._count = count
        self._state = state
        self.polls = 0

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        self.polls += 1
        base = super().list_open_prs(repo)[0]
        return [replace(base, number=n, head_sha=f"head{n}") for n in range(1, self._count + 1)]

    def get_pull_request(self, repo: str, number: int) -> PullRequest:
        base = super().list_open_prs(repo)[0]
        return replace(base, number=number, head_sha=f"head{number}", state=self._state)


class GatedProvider:
    """Holds every review until released, counting how many run at once."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.running = 0
        self.peak = 0
        self.done = 0
        self._lock = threading.Lock()

    def start_review_session(self, job, worktree, timeout_seconds) -> SessionRef:
        return SessionRef(id=str(job.id), container="c", worktree=worktree)

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        self.release.wait(10)
        with self._lock:
            self.running -= 1
            self.done += 1
        return ReviewResult(body="ok", event="APPROVE")

    def cleanup(self, session: SessionRef) -> None:
        pass

    def discard(self, job) -> None:
        pass


def _worker(tmp_path: Path, github, provider, concurrency: int, **kwargs) -> ReviewWorker:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    return ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=github,
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers={"codex": provider},
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
        concurrency=concurrency,
        **kwargs,
    )


def _until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_runs_up_to_the_concurrency_limit_and_drains_the_rest(tmp_path: Path) -> None:
    gh = ManyPRs(5)
    provider = GatedProvider()
    worker = _worker(tmp_path, gh, provider, concurrency=3)
    worker.start()

    worker.tick()

    assert _until(lambda: provider.running == 3)
    time.sleep(0.1)
    assert provider.running == 3, "never more than the limit"
    provider.release.set()
    assert _until(lambda: len(worker.store.list_by_status(JobStatus.POSTED)) == 5), (
        "the queue drains without another tick"
    )
    assert provider.peak == 3
    assert sorted(commit for *_, commit in gh.posted) == [f"head{n}" for n in range(1, 6)]


def test_polling_never_waits_behind_running_reviews(tmp_path: Path) -> None:
    gh = ManyPRs(2)
    provider = GatedProvider()
    worker = _worker(tmp_path, gh, provider, concurrency=1)
    worker.start()
    worker.tick()
    assert _until(lambda: provider.running == 1)

    started = time.time()
    worker.tick()
    worker.tick()

    assert time.time() - started < 1, "a tick returns while a review is still running"
    assert gh.polls == 3
    provider.release.set()
    assert _until(lambda: provider.done == 2)


def test_a_closed_pr_is_skipped_not_reviewed(tmp_path: Path) -> None:
    gh = ManyPRs(1, state="closed")
    provider = GatedProvider()
    provider.release.set()
    worker = _worker(tmp_path, gh, provider, concurrency=1)
    worker.store.enqueue(REPO, "homelab", "codex", 1, "head1", "base")

    assert worker.run_next()

    job = worker.store.find(REPO, 1, "head1")
    assert job is not None
    assert (job.status, job.last_error) == (JobStatus.SKIPPED, "PR closed")
    assert provider.done == 0 and gh.posted == [] and gh.comments == []


def test_start_requeues_reviews_a_restart_interrupted(tmp_path: Path) -> None:
    """The prod orphans (jobs left `running` by a restart in August) and every
    later one: back to the queue at no cost in attempts. The still-open PR is
    reviewed; a closed PR is skipped on its first claim, never reviewed."""
    gh = ManyPRs(1)
    provider = GatedProvider()
    provider.release.set()
    worker = _worker(tmp_path, gh, provider, concurrency=2)
    store = worker.store
    orphan = store.enqueue(REPO, "homelab", "codex", 1, "head1", "base")
    begin_attempt(store, orphan.id)

    worker.start()
    worker.tick()

    assert _until(lambda: (j := store.get(orphan.id)) is not None and j.status == JobStatus.POSTED)
    assert store.get(orphan.id).attempts == 1  # type: ignore[union-attr]


def test_outage_bookkeeping_is_shared_safely_across_runners(tmp_path: Path) -> None:
    class Down:
        def start_review_session(self, job, worktree, timeout_seconds) -> SessionRef:
            raise ProviderUnavailable("codex", "exhausted", "usage limit")

        def run_review(self, session, prompt, timeout_seconds) -> ReviewResult:
            raise AssertionError

        def cleanup(self, session) -> None:
            pass

        def discard(self, job) -> None:
            pass

    worker = _worker(tmp_path, ManyPRs(6), Down(), concurrency=3)
    worker.start()
    worker.tick()

    assert _until(lambda: len(worker.store.list_by_status(JobStatus.FAILED)) == 6)
    assert [o["provider"] for o in worker.degraded_providers()] == ["codex"]


def test_concurrency_below_one_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        _worker(tmp_path, ManyPRs(1), GatedProvider(), concurrency=0)


def test_start_removes_the_containers_a_restart_left_behind_before_requeuing(
    tmp_path: Path,
) -> None:
    """A restart does not stop the dead process's review containers, and the
    requeued job's next session reuses their names, so it would fail on a name
    conflict, with no SessionRef to clean up after. Each provider discards its
    session for every interrupted job before anything is requeued."""
    from unittest.mock import MagicMock

    from docker.errors import NotFound
    from src.config import ClaudeAccount, CodexAccount, CredentialSource
    from src.providers.claude import ClaudeReviewProvider
    from src.providers.codex import CodexReviewProvider

    docker = MagicMock()
    left_behind = {
        "review-test-codex-1": MagicMock(),
        "review-test-claude-work-1": MagicMock(),
    }

    def get(name: str):
        if name not in left_behind:
            raise NotFound(name)
        return left_behind[name]

    docker.containers.get.side_effect = get
    providers = {
        "codex": CodexReviewProvider(
            account="codex",
            settings=CodexAccount(
                kind="codex", credential=CredentialSource(file=str(tmp_path / "auth.json"))
            ),
            docker_client=docker,
            projects_root=tmp_path / "projects",
            image="codex:test",
            instance="test",
        ),
        "claude-work": ClaudeReviewProvider(
            account="claude-work",
            settings=ClaudeAccount(
                kind="claude",
                auth="subscription",
                credential=CredentialSource(env="T"),
                model="m",
            ),
            docker_client=docker,
            worktrees=MagicMock(),
            projects_root=tmp_path / "projects",
            image="claude:test",
            instance="test",
        ),
    }
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    orphan = store.enqueue(REPO, "homelab", "codex", 1, "head1", "base")
    begin_attempt(store, orphan.id)
    worker = ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=ManyPRs(1),
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers=providers,  # type: ignore[arg-type]
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
        concurrency=1,
    )
    seen_at_removal: list[JobStatus] = []
    for container in left_behind.values():
        container.remove.side_effect = lambda **_: seen_at_removal.append(
            store.get(orphan.id).status  # type: ignore[union-attr]
        )

    worker.start()

    assert all(c.remove.call_args.kwargs == {"force": True} for c in left_behind.values())
    assert seen_at_removal == [JobStatus.RUNNING, JobStatus.RUNNING], "removed before requeue"
    assert store.get(orphan.id).status == JobStatus.QUEUED  # type: ignore[union-attr]


def test_an_orphan_whose_container_will_not_go_stays_running_until_it_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Stubborn(GatedProvider):
        def __init__(self) -> None:
            super().__init__()
            self.release.set()
            self.refusals = 1

        def discard(self, job) -> None:
            if self.refusals:
                self.refusals -= 1
                raise RuntimeError("docker daemon busy")

    provider = Stubborn()
    worker = _worker(tmp_path, ManyPRs(1), provider, concurrency=1)
    store = worker.store
    orphan = store.enqueue(REPO, "homelab", "codex", 1, "head1", "base")
    begin_attempt(store, orphan.id)

    worker.start()

    assert store.get(orphan.id).status == JobStatus.RUNNING  # type: ignore[union-attr]
    assert "docker daemon busy" in capsys.readouterr().err
    assert provider.done == 0, "its PR stays unclaimable while the old session may live"

    worker.tick()

    assert _until(lambda: store.get(orphan.id).status == JobStatus.POSTED)  # type: ignore[union-attr]
    assert store.get(orphan.id).attempts == 1  # type: ignore[union-attr]


def test_a_review_that_keeps_killing_the_service_stops_being_refunded(tmp_path: Path) -> None:
    """Two restarts mid-review were refunded; the third counts as the attempt it
    was, and on the last attempt says so on the PR like any failed review."""
    gh = ManyPRs(1)
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    job = store.enqueue(REPO, "homelab", "codex", 1, "head1", "base")
    for _ in range(2):
        begin_attempt(store, job.id)
        store.recover_interrupted(job.id, 2, "interrupted by a restart")
    begin_attempt(store, job.id)  # running again when the process dies a third time
    worker = _worker(
        tmp_path, gh, GatedProvider(), concurrency=1, max_attempts=1, max_restart_refunds=2
    )

    worker.start()

    after = store.get(job.id)
    assert after is not None
    assert (after.status, after.attempts, after.restart_refunds) == (JobStatus.FAILED, 1, 2)
    assert len(gh.comments) == 1
    body = gh.comments[0][2]
    assert body.startswith(NO_REVIEW_MARKER)
    assert "counted as a failed attempt" in body


def _age_claim(tmp_path: Path, job_id: int, seconds: float) -> None:
    with sqlite3.connect(tmp_path / "jobs.db") as conn:
        conn.execute(
            "UPDATE review_jobs SET started_at = ? WHERE id = ?", (time.time() - seconds, job_id)
        )


def test_a_job_stuck_running_with_no_runner_is_recovered_by_the_next_tick(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = GatedProvider()
    provider.release.set()
    worker = _worker(tmp_path, ManyPRs(1), provider, concurrency=1, stuck_running_seconds=1801)
    store = worker.store
    lost = store.enqueue(REPO, "homelab", "codex", 1, "head1", "base")
    begin_attempt(store, lost.id)  # running, but no runner of this worker holds it
    _age_claim(tmp_path, lost.id, 600)

    worker.tick()
    assert store.get(lost.id).status == JobStatus.RUNNING, "not stuck yet"  # type: ignore[union-attr]

    _age_claim(tmp_path, lost.id, 1802)
    worker.tick()

    recovered = store.get(lost.id)
    assert recovered is not None
    assert (recovered.status, recovered.attempts, recovered.restart_refunds) == (
        JobStatus.QUEUED,
        0,
        1,
    )
    assert recovered.last_error is not None and "stuck running" in recovered.last_error
    assert "stuck running with no runner" in capsys.readouterr().err


def test_a_long_review_a_runner_still_holds_is_never_reaped(tmp_path: Path) -> None:
    provider = GatedProvider()
    worker = _worker(tmp_path, ManyPRs(1), provider, concurrency=1, stuck_running_seconds=1801)
    store = worker.store
    worker.start()
    worker.tick()
    assert _until(lambda: provider.running == 1)
    job = store.list_by_status(JobStatus.RUNNING)[0]
    _age_claim(tmp_path, job.id, 99_999)

    worker.tick()
    assert store.get(job.id).status == JobStatus.RUNNING  # type: ignore[union-attr]

    provider.release.set()
    assert _until(lambda: store.get(job.id).status == JobStatus.POSTED)  # type: ignore[union-attr]
    assert store.get(job.id).restart_refunds == 0  # type: ignore[union-attr]


def test_a_stuck_timeout_inside_a_legitimate_review_is_a_config_error(tmp_path: Path) -> None:
    # config(): max_runtime_minutes 30, one provider -> a review may run 1800 s.
    with pytest.raises(ValueError, match="stuck_running_seconds"):
        _worker(tmp_path, ManyPRs(1), GatedProvider(), concurrency=1, stuck_running_seconds=1800)


# ── the runner loop itself ───────────────────────────────────────────────────
# These drive `start()`'s real runner threads, not `tick_and_drain`, so they fail
# if the production loop loses a wake, its pinned clock, or itself.


def test_a_wake_during_an_empty_scan_is_not_lost(tmp_path: Path) -> None:
    gh = ManyPRs(1)
    provider = GatedProvider()
    provider.release.set()
    worker = _worker(tmp_path, gh, provider, concurrency=1)
    real_run_next = worker.run_next
    first = threading.Event()

    def run_next(as_of=None) -> bool:
        if not first.is_set():
            # The queue is empty when scanned; the tick lands before the
            # runner goes to sleep.
            first.set()
            worker.tick()
            return False
        return real_run_next(as_of)

    worker.run_next = run_next  # type: ignore[method-assign]
    worker.start()

    assert _until(lambda: len(worker.store.list_by_status(JobStatus.POSTED)) == 1), (
        "the job enqueued by that tick runs without waiting for another"
    )
    assert gh.polls == 1


def test_a_job_that_fails_mid_drain_waits_for_the_next_wake(tmp_path: Path) -> None:
    class FailsOnce(GatedProvider):
        def __init__(self) -> None:
            super().__init__()
            self.release.set()
            self.calls = 0

        def run_review(self, session, prompt, timeout_seconds) -> ReviewResult:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            return super().run_review(session, prompt, timeout_seconds)

    provider = FailsOnce()
    worker = _worker(tmp_path, ManyPRs(1), provider, concurrency=1)  # backoff 0
    worker.start()
    worker.tick()

    assert _until(lambda: len(worker.store.list_by_status(JobStatus.FAILED)) == 1)
    time.sleep(0.2)
    assert provider.calls == 1, "a zero backoff must not retry inside the same drain"

    worker.tick()

    assert _until(lambda: len(worker.store.list_by_status(JobStatus.POSTED)) == 1)
    assert provider.calls == 2
    assert worker.store.list_by_status(JobStatus.POSTED)[0].attempts == 2


def test_a_runner_survives_a_failed_pass_and_resumes_on_the_next_wake(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = GatedProvider()
    provider.release.set()
    worker = _worker(tmp_path, ManyPRs(1), provider, concurrency=1)
    store = worker.store
    real_list = store.list_retryable
    failures = [sqlite3.OperationalError("database is locked")]

    def flaky_list(*args, **kwargs):
        if failures:
            raise failures.pop()
        return real_list(*args, **kwargs)

    store.list_retryable = flaky_list  # type: ignore[method-assign]
    worker.start()
    assert _until(lambda: "database is locked" in capsys.readouterr().err)

    worker.tick()

    assert _until(lambda: len(store.list_by_status(JobStatus.POSTED)) == 1)
    assert store.list_by_status(JobStatus.POSTED)[0].attempts == 1
