from dataclasses import replace
from pathlib import Path

import pytest
from src.config import ReviewConfig
from src.github_client import PullRequest
from src.job_store import JobStatus, ReviewJobStore
from src.provider import ReviewResult, SessionRef
from src.worker import NO_REVIEW_MARKER, ReviewWorker

from tests.helpers import begin_attempt, tick_and_drain, write_config


class FakeGitHub:
    def __init__(self) -> None:
        self.posted: list[tuple[str, int, str, str, str]] = []
        self.comments: list[tuple[str, int, str]] = []

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

    def head_ci_green(self, repo: str, head_sha: str) -> bool:
        return False

    def post_review(self, repo: str, number: int, body: str, event: str, commit_id: str) -> None:
        self.posted.append((repo, number, body, event, commit_id))

    def post_comment(self, repo: str, number: int, body: str) -> None:
        self.comments.append((repo, number, body))


class FakeWorktrees:
    def __init__(self, path: Path) -> None:
        self.path = path

    def pr_diff(self, *, repo_dir: Path, base_sha: str, head_sha: str) -> str | None:
        return f"diff --git a/README.md b/README.md\n+{head_sha}\n"

    def diff_since(self, *, repo_dir: Path, previous_head_sha: str, head_sha: str) -> str | None:
        return f"+pushed {previous_head_sha}..{head_sha}\n"

    def prepare(
        self, *, repo_dir: Path, repo: str, project: str, pr_number: int, head_sha: str
    ) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        return self.path

    def cleanup(self, *, repo_dir: Path, worktree: Path) -> None:
        pass


class FakeProvider:
    def start_review_session(self, job, worktree, timeout_seconds) -> SessionRef:
        return SessionRef(id=str(job.id), container="c", worktree=worktree)

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        assert "You are reviewing PR #1" in prompt
        return ReviewResult(body="No findings.", event="COMMENT")

    def cleanup(self, session: SessionRef) -> None:
        pass


def config(tmp_path: Path) -> ReviewConfig:
    return write_config(tmp_path)


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
    tick_and_drain(worker)
    assert store.list_by_status(JobStatus.POSTED)[0].head_sha == "head"
    assert gh.posted == [("alvaro-francisco-gil/homelab", 1, "No findings.", "COMMENT", "head")]


class FlakyProvider:
    def __init__(self) -> None:
        self.calls = 0

    def start_review_session(self, job, worktree, timeout_seconds) -> SessionRef:
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
    tick_and_drain(worker)  # first attempt fails
    assert store.list_by_status(JobStatus.FAILED), "first attempt should have failed"
    assert not store.list_by_status(JobStatus.POSTED)

    tick_and_drain(worker)  # retry succeeds
    posted = store.list_by_status(JobStatus.POSTED)
    assert posted and posted[0].head_sha == "head"
    assert provider.calls == 2
    assert gh.posted == [("alvaro-francisco-gil/homelab", 1, "No findings.", "COMMENT", "head")]


class FailingProvider:
    def start_review_session(self, job, worktree, timeout_seconds) -> SessionRef:
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
    tick_and_drain(worker)

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
        tick_and_drain(worker)

    err = capsys.readouterr().err
    assert "3 consecutive failures" in err, (
        "a chronically failing repo must be distinguishable from a one-off failure"
    )


def test_a_review_out_of_attempts_says_so_on_the_pr(tmp_path: Path) -> None:
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
        max_attempts=2,
    )
    tick_and_drain(worker)
    assert gh.comments == [], "a failure with retries left is not news yet"

    tick_and_drain(worker)
    assert len(gh.comments) == 1
    _, number, body = gh.comments[0]
    assert number == 1
    assert body.startswith(NO_REVIEW_MARKER)
    assert "codex boom" in body

    tick_and_drain(worker)
    assert len(gh.comments) == 1, "an exhausted job is not retried, so nothing new to say"


class ApprovingProvider:
    def start_review_session(self, job, worktree, timeout_seconds) -> SessionRef:
        return SessionRef(id=str(job.id), container="c", worktree=worktree)

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        return ReviewResult(body="No findings.", event="APPROVE")

    def cleanup(self, session: SessionRef) -> None:
        pass


def test_request_review_queues_the_current_head_without_running_it(tmp_path: Path) -> None:
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
    )
    job = worker.request_review("alvaro-francisco-gil/homelab", 1)
    assert (job.head_sha, job.status) == ("head", JobStatus.QUEUED)
    assert gh.posted == [], "the request returns before any review runs"

    assert worker.request_review("alvaro-francisco-gil/homelab", 1).id == job.id, "idempotent"

    tick_and_drain(worker)
    done = store.get(job.id)
    assert done is not None and (done.status, done.verdict) == (JobStatus.POSTED, "APPROVE")


def test_request_review_retries_a_head_that_ran_out_of_attempts(tmp_path: Path) -> None:
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
        max_attempts=1,
    )
    tick_and_drain(worker)
    assert store.list_by_status(JobStatus.FAILED)

    job = worker.request_review("alvaro-francisco-gil/homelab", 1)

    assert (job.status, job.attempts) == (JobStatus.QUEUED, 0)


def test_request_review_rejects_a_repo_with_no_policy(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    worker = ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=FakeGitHub(),
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers={"codex": ApprovingProvider()},
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
    )
    with pytest.raises(KeyError):
        worker.request_review("someone/else", 1)


class BombProvider:
    """Raises unconditionally — proves the provider was never invoked."""

    def start_review_session(self, job, worktree, timeout_seconds) -> SessionRef:
        raise AssertionError("provider must not be called")

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        raise AssertionError("provider must not be called")

    def cleanup(self, session: SessionRef) -> None:
        pass


class EmptyGitHub(FakeGitHub):
    """FakeGitHub that returns no open PRs so poll_once enqueues nothing."""

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        return []

    def get_pull_request(self, repo: str, number: int) -> PullRequest:
        return replace(FakeGitHub().get_pull_request(repo, number), head_sha="head-v2")


def test_tick_skips_jobs_over_round_cap(tmp_path: Path) -> None:
    """A claimed job is skipped when rounds_for >= max_rounds (Fix #1).

    Regression: before the fix, the worker ran every retryable job without checking
    the round cap, so an always-on poller would keep reviewing new head SHAs past
    MAX_REVIEW_ROUNDS.
    """
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()

    # Seed one POSTED round at head_sha="head-v1" — rounds_for == 1.
    j1 = store.enqueue("alvaro-francisco-gil/homelab", "homelab", "codex", 1, "head-v1", "base")
    begin_attempt(store, j1.id)
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

    tick_and_drain(worker)

    assert store.get(j2.id).status == JobStatus.SKIPPED, "over-cap job should be SKIPPED, not run"
    assert all(commit_id != "head-v2" for (_, _, _, _, commit_id) in gh.posted), (
        "post_review must not be called for the over-cap job"
    )


def test_a_failed_review_is_not_retried_inside_the_backoff(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    provider = FlakyProvider()
    worker = ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=FakeGitHub(),
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers={"codex": provider},
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
        retry_backoff_seconds=900,
    )
    tick_and_drain(worker)
    tick_and_drain(worker)
    assert provider.calls == 1, "the next 60 s tick must not spend another attempt"


class TwoPRsOneGreen(FakeGitHub):
    """PR 1 is older; only PR 2's head has passed CI."""

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        base = super().list_open_prs(repo)[0]
        return [replace(base, number=n, head_sha=f"head{n}") for n in (1, 2)]

    def get_pull_request(self, repo: str, number: int) -> PullRequest:
        return next(p for p in self.list_open_prs(repo) if p.number == number)

    def head_ci_green(self, repo: str, head_sha: str) -> bool:
        return head_sha == "head2"


class AnyPRProvider(ApprovingProvider):
    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        return ReviewResult(body="ok", event="APPROVE")


def test_a_head_with_green_ci_is_reviewed_before_an_older_pr(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    gh = TwoPRsOneGreen()
    worker = ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=gh,
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers={"codex": AnyPRProvider()},
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
    )
    tick_and_drain(worker)
    assert [number for (_, number, _, _, _) in gh.posted] == [2, 1]


def test_unreadable_ci_leaves_the_queue_in_pr_age_order(tmp_path: Path) -> None:
    class Forbidden(TwoPRsOneGreen):
        def head_ci_green(self, repo: str, head_sha: str) -> bool:
            raise RuntimeError("HTTP 403 Resource not accessible by integration")

    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    gh = Forbidden()
    worker = ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=gh,
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers={"codex": AnyPRProvider()},
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
    )
    tick_and_drain(worker)
    assert [number for (_, number, _, _, _) in gh.posted] == [1, 2]


class MovingHeadGitHub(FakeGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.head = "head"

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        return [replace(pr, head_sha=self.head) for pr in super().list_open_prs(repo)]


class PromptRecordingProvider(FakeProvider):
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        self.prompts.append(prompt)
        return ReviewResult(body="1. `README.md:1` is wrong.", event="REQUEST_CHANGES")


def test_a_later_round_builds_on_the_last_review_instead_of_starting_over(
    tmp_path: Path,
) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    gh = MovingHeadGitHub()
    provider = PromptRecordingProvider()
    worker = ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=gh,
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers={"codex": provider},
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
    )
    tick_and_drain(worker)
    gh.head = "head2"
    tick_and_drain(worker)

    first, second = provider.prompts
    assert "```diff\ndiff --git a/README.md b/README.md\n+head\n```" in first
    assert "Earlier round" not in first
    assert "Earlier round: head head got REQUEST_CHANGES" in second
    assert "1. `README.md:1` is wrong." in second
    assert "+pushed head..head2" in second
    # The body kept is the provider's own, not the disclosure-wrapped one posted.
    assert store.find("alvaro-francisco-gil/homelab", 1, "head").review_body == (
        "1. `README.md:1` is wrong."
    )
