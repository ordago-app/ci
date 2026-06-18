from pathlib import Path

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
            repo: alvaro/homelab
            project: homelab
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
    assert gh.posted == [("alvaro/homelab", 1, "No findings.", "COMMENT", "head")]


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
    assert gh.posted == [("alvaro/homelab", 1, "No findings.", "COMMENT", "head")]


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
    summary = worker.run_pr_review("alvaro/homelab", 1)
    assert summary.verdict == "APPROVE"
    assert summary.head_sha == "head"
    assert summary.escalated is False
    assert gh.posted == [("alvaro/homelab", 1, "No findings.", "APPROVE", "head")]
    # Verdict persisted so a repeat request is idempotent.
    posted = store.list_by_status(JobStatus.POSTED)
    assert posted and posted[0].verdict == "APPROVE"
