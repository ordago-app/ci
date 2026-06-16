from pathlib import Path

from src.config import ReviewConfig
from src.github_client import PullRequest
from src.job_store import JobStatus, ReviewJobStore
from src.poller import ReviewPoller


class FakeGitHub:
    def __init__(self, prs: list[PullRequest]) -> None:
        self.prs = prs

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        return self.prs


def config(tmp_path: Path, *, review_drafts: bool = False) -> ReviewConfig:
    path = tmp_path / "agent-review.yml"
    path.write_text(
        f"""
        repos:
          homelab:
            repo: alvaro/homelab
            project: homelab
            provider: codex
            enabled: true
            review_drafts: {str(review_drafts).lower()}
            run_ci_first: true
            tool_profile: reviewer
        tool_profiles:
          reviewer:
            github_role: reviewer
            mcps: {{}}
            allow_shell: true
            allow_network: true
            allow_repo_write: true
            allow_git_push: false
            allow_docker: false
            max_runtime_minutes: 30
        """
    )
    return ReviewConfig.load(path)


def pr(*, draft: bool = False, author: str = "alice", sha: str = "head") -> PullRequest:
    return PullRequest(
        number=9,
        title="Title",
        body="Body",
        draft=draft,
        state="open",
        author=author,
        base_ref="main",
        base_sha="base",
        head_ref="feature",
        head_sha=sha,
    )


def test_enqueues_new_open_pr_head_sha(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    poller = ReviewPoller(config(tmp_path), store, FakeGitHub([pr()]), reviewer_bot="reviewer[bot]")
    created = poller.poll_once()
    assert len(created) == 1
    assert store.list_by_status(JobStatus.QUEUED)[0].head_sha == "head"


def test_skips_draft_when_policy_disables_drafts(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    poller = ReviewPoller(config(tmp_path), store, FakeGitHub([pr(draft=True)]), reviewer_bot="reviewer[bot]")
    assert poller.poll_once() == []


def test_skips_reviewer_bot_author(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    poller = ReviewPoller(config(tmp_path), store, FakeGitHub([pr(author="reviewer[bot]")]), reviewer_bot="reviewer[bot]")
    assert poller.poll_once() == []


def test_new_sha_gets_second_job(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    gh = FakeGitHub([pr(sha="one")])
    poller = ReviewPoller(config(tmp_path), store, gh, reviewer_bot="reviewer[bot]")
    poller.poll_once()
    gh.prs = [pr(sha="two")]
    poller.poll_once()
    assert [job.head_sha for job in store.list_by_status(JobStatus.QUEUED)] == ["one", "two"]
