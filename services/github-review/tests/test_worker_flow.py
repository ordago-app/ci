from pathlib import Path

import pytest
from src.config import ReviewConfig
from src.github_client import PullRequest
from src.job_store import JobStatus, ReviewJobStore
from src.provider import ReviewResult, SessionRef
from src.worker import ReviewWorker


class FakeGitHub:
    def __init__(self) -> None:
        self.posted: list[tuple[str, int, str, str, str]] = []

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        return [
            PullRequest(
                number=1,
                title="Title",
                body="Body",
                draft=False,
                state="open",
                author="alice",
                base_ref="main",
                base_sha="base",
                head_ref="feature",
                head_sha="head",
            )
        ]

    def get_pull_request(self, repo: str, number: int) -> PullRequest:
        return self.list_open_prs(repo)[0]

    def changed_files(self, repo: str, number: int) -> list[str]:
        return ["README.md"]

    def diffstat(self, repo: str, number: int) -> str:
        return "1 file changed"

    def ci_summary(self, repo: str, head_sha: str) -> str:
        return "checks skipped in test"

    def post_review(self, repo: str, number: int, body: str, event: str, commit_id: str) -> None:
        self.posted.append((repo, number, body, event, commit_id))


class FakeWorktrees:
    def __init__(self, path: Path) -> None:
        self.path = path

    def prepare(self, *, repo_dir: Path, project: str, pr_number: int, head_sha: str) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        return self.path

    def cleanup(self, worktree: Path) -> None:
        pass


class FakeProvider:
    def start_review_session(self, job, worktree, profile) -> SessionRef:
        return SessionRef(id=str(job.id), container="c", worktree=worktree)

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        assert "You are reviewing PR #1" in prompt
        return ReviewResult(body="No findings.", event="COMMENT")

    def cleanup(self, session: SessionRef) -> None:
        pass


def config(tmp_path: Path) -> ReviewConfig:
    path = tmp_path / "agent-review.yml"
    path.write_text(
        """
        repos:
          homelab:
            provider: codex
            enabled: true
            review_drafts: false
            run_ci_first: true
            tool_profile: reviewer
        tool_profiles:
          reviewer:
            github_role: reviewer
            mcps: {}
            allow_shell: true
            allow_network: true
            allow_repo_write: true
            allow_git_push: false
            allow_docker: false
            max_runtime_minutes: 30
        """
    )
    return ReviewConfig.load(path)


def test_worker_polls_runs_review_and_marks_posted(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    gh = FakeGitHub()
    worker = ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=gh,
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers={"codex": FakeProvider()},
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
    )
    worker.tick()
    assert store.list_by_status(JobStatus.POSTED)[0].head_sha == "head"
    assert gh.posted == [("alvaro-francisco-gil/homelab", 1, "No findings.", "COMMENT", "head")]


class FlakyProvider:
    def __init__(self) -> None:
        self.calls = 0

    def start_review_session(self, job, worktree, profile) -> SessionRef:
        return SessionRef(id=str(job.id), container="c", worktree=worktree)

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient codex failure")
        return ReviewResult(body="No findings.", event="COMMENT")

    def cleanup(self, session: SessionRef) -> None:
        pass


def test_worker_retries_failed_job_on_next_tick(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    gh = FakeGitHub()
    provider = FlakyProvider()
    worker = ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=gh,
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers={"codex": provider},
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
        max_attempts=3,
    )
    worker.tick()  # first attempt fails
    assert store.list_by_status(JobStatus.FAILED), "first attempt should have failed"
    assert not store.list_by_status(JobStatus.POSTED)

    worker.tick()  # retry succeeds
    posted = store.list_by_status(JobStatus.POSTED)
    assert posted and posted[0].head_sha == "head"
    assert provider.calls == 2
    assert gh.posted == [("alvaro-francisco-gil/homelab", 1, "No findings.", "COMMENT", "head")]


class FailingProvider:
    def start_review_session(self, job, worktree, profile) -> SessionRef:
        return SessionRef(id=str(job.id), container="c", worktree=worktree)

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        raise RuntimeError("codex boom")

    def cleanup(self, session: SessionRef) -> None:
        pass


def test_a_failed_job_is_logged_not_just_recorded_in_the_db(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Regression: `_run_job` caught every exception and put it in the job row's
    # last_error, and nowhere else. github-review failed 15/15 reviews on
    # ordago-apps from 2026-06-21 with a `git worktree add … Permission denied`
    # and posted zero reviews — and nobody noticed for seven weeks, because the
    # only record was a column in a SQLite file on the host. `docker logs
    # github-review` was clean.
    #
    # A failure that is written only where nobody looks is a silent fallback.
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    worker = ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=FakeGitHub(),
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers={"codex": FailingProvider()},
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
    )
    worker.tick()

    assert store.list_by_status(JobStatus.FAILED), "precondition: the job must have failed"
    err = capsys.readouterr().err
    assert "codex boom" in err, "the cause must reach the container log, not just last_error"
    assert "alvaro-francisco-gil/homelab" in err, (
        "the log must name the repo so a broken repo is greppable"
    )
    assert "#1" in err, "the log must name the PR"


def test_a_repo_failing_over_and_over_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # One failure is normal (a force-push mid-review, a flaky container). A repo
    # that fails every single time is a broken deployment, and the two must not
    # look identical in the log — that is exactly how ordago-apps stayed broken
    # for seven weeks. Carry the consecutive-failure count so the second kind is
    # greppable on its own.
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    worker = ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=FakeGitHub(),
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers={"codex": FailingProvider()},
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
        max_attempts=10,
    )
    for _ in range(3):
        worker.tick()

    err = capsys.readouterr().err
    assert "3 consecutive failures" in err, (
        "a chronically failing repo must be distinguishable from a one-off failure"
    )


def test_run_pr_review_raises_when_review_fails(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    gh = FakeGitHub()
    worker = ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=gh,
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers={"codex": FailingProvider()},
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
        max_attempts=3,
    )
    with pytest.raises(RuntimeError):
        worker.run_pr_review("alvaro-francisco-gil/homelab", 1)


class ApprovingProvider:
    def start_review_session(self, job, worktree, profile) -> SessionRef:
        return SessionRef(id=str(job.id), container="c", worktree=worktree)

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        return ReviewResult(body="No findings.", event="APPROVE")

    def cleanup(self, session: SessionRef) -> None:
        pass


def test_run_pr_review_returns_verdict_summary(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    gh = FakeGitHub()
    worker = ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=gh,
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers={"codex": ApprovingProvider()},
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
        max_attempts=3,
    )
    summary = worker.run_pr_review("alvaro-francisco-gil/homelab", 1)
    assert summary.verdict == "APPROVE"
    assert summary.head_sha == "head"
    assert summary.escalated is False
    assert gh.posted == [("alvaro-francisco-gil/homelab", 1, "No findings.", "APPROVE", "head")]
    # Verdict persisted so a repeat request is idempotent.
    posted = store.list_by_status(JobStatus.POSTED)
    assert posted and posted[0].verdict == "APPROVE"


class BombProvider:
    """Raises unconditionally — proves the provider was never invoked."""

    def start_review_session(self, job, worktree, profile) -> SessionRef:
        raise AssertionError("provider must not be called")

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        raise AssertionError("provider must not be called")

    def cleanup(self, session: SessionRef) -> None:
        pass


class EmptyGitHub(FakeGitHub):
    """FakeGitHub that returns no open PRs so poll_once enqueues nothing."""

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        return []


def test_tick_skips_jobs_over_round_cap(tmp_path: Path) -> None:
    """tick() must skip a queued job when rounds_for >= max_rounds (Fix #1).

    Regression: before the fix, tick() ran every retryable job without checking
    the round cap, so an always-on poller would keep reviewing new head SHAs past
    MAX_REVIEW_ROUNDS.
    """
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()

    # Seed one POSTED round at head_sha="head-v1" — rounds_for == 1.
    j1 = store.enqueue("alvaro-francisco-gil/homelab", "homelab", "codex", 1, "head-v1", "base")
    store.mark_running(j1.id)
    store.mark_posted(j1.id, verdict="REQUEST_CHANGES")
    assert store.rounds_for("alvaro-francisco-gil/homelab", 1) == 1

    # Enqueue a new QUEUED job for the same PR at a different head.
    j2 = store.enqueue("alvaro-francisco-gil/homelab", "homelab", "codex", 1, "head-v2", "base")
    assert store.get(j2.id).status == JobStatus.QUEUED

    # EmptyGitHub so poll_once doesn't enqueue extra jobs.
    gh = EmptyGitHub()
    worker = ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=gh,
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers={"codex": BombProvider()},
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
        max_rounds=1,  # cap already reached after the seed job
    )

    worker.tick()

    assert store.get(j2.id).status == JobStatus.SKIPPED, "over-cap job should be SKIPPED, not run"
    assert all(commit_id != "head-v2" for (_, _, _, _, commit_id) in gh.posted), (
        "post_review must not be called for the over-cap job"
    )


def test_run_pr_review_idempotent_for_legacy_posted_row_without_verdict(tmp_path: Path) -> None:
    """run_pr_review must treat ANY POSTED row as already-reviewed (Fix #2).

    Regression: the old guard was `existing.verdict is not None`, so a legacy
    migrated row (status=POSTED, verdict=NULL) bypassed the guard and triggered
    a duplicate review + duplicate post_review call.
    """
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()

    # FakeGitHub returns head_sha="head"; seed a POSTED row for that head with
    # verdict=None (simulating a legacy migrated row).
    j1 = store.enqueue("alvaro-francisco-gil/homelab", "homelab", "codex", 1, "head", "base")
    store.mark_running(j1.id)
    store.mark_posted(j1.id)  # no verdict arg → verdict stays NULL
    assert store.get_posted("alvaro-francisco-gil/homelab", 1, "head").verdict is None

    gh = FakeGitHub()
    worker = ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=gh,
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers={"codex": BombProvider()},
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
    )

    summary = worker.run_pr_review("alvaro-francisco-gil/homelab", 1)

    assert summary.verdict == "REQUEST_CHANGES", "conservative fallback for legacy NULL verdict"
    assert summary.escalated is False
    assert gh.posted == [], "must not post a duplicate review"
