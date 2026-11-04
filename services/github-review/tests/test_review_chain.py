"""A repo's fallback providers review a head its primary provider cannot.

The reviewer is a merge gate, so a primary provider out of quota used to mean no
PR could land until the quota reset. A fallback spends real credit, so it runs
only when the primary cannot answer: never because the primary disagreed, and
never for a failure the next tick's retry would have fixed for free.
"""

from pathlib import Path

from src.config import ReviewConfig
from src.job_store import JobStatus, ReviewJob, ReviewJobStore
from src.provider import ProviderUnavailableError, ReviewResult, SessionRef
from src.worker import ReviewWorker

from .test_worker_flow import FakeGitHub, FakeWorktrees


class ScriptedProvider:
    """Answers each run_review with the next scripted outcome."""

    def __init__(self, *outcomes: ReviewResult | Exception, start_error: Exception | None = None):
        self.outcomes = list(outcomes)
        self.start_error = start_error
        self.runs = 0
        self.cleaned: list[str] = []

    def start_review_session(self, job: ReviewJob, worktree: Path, profile) -> SessionRef:
        if self.start_error is not None:
            raise self.start_error
        return SessionRef(id=str(job.id), container=f"c-{job.id}", worktree=worktree)

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        self.runs += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def cleanup(self, session: SessionRef) -> None:
        self.cleaned.append(session.container)


APPROVE = ReviewResult(body="No findings.", event="APPROVE")
FINDINGS = ReviewResult(body="`a.py:1` is wrong.", event="REQUEST_CHANGES")


def _config(tmp_path: Path, *, fallbacks: str = "[claude]") -> ReviewConfig:
    path = tmp_path / "agent-review.yml"
    path.write_text(
        f"""
        repos:
          homelab:
            provider: codex
            fallback_providers: {fallbacks}
            tool_profile: reviewer
        tool_profiles:
          reviewer:
            github_role: reviewer
            allow_git_push: false
        """
    )
    return ReviewConfig.load(path)


def _worker(
    tmp_path: Path,
    codex: ScriptedProvider,
    claude: ScriptedProvider,
    *,
    fallbacks: str = "[claude]",
    max_attempts: int = 3,
) -> tuple[ReviewWorker, FakeGitHub, ReviewJobStore]:
    github = FakeGitHub()
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    worker = ReviewWorker(
        config=_config(tmp_path, fallbacks=fallbacks),
        store=store,
        github=github,
        worktrees=FakeWorktrees(tmp_path / "worktree"),
        providers={"codex": codex, "claude": claude},
        projects_root=tmp_path,
        reviewer_bot="reviewer[bot]",
        max_attempts=max_attempts,
    )
    return worker, github, store


def _only_job(store: ReviewJobStore, status: JobStatus) -> ReviewJob:
    jobs = store.list_by_status(status)
    assert len(jobs) == 1
    return jobs[0]


def test_primary_answering_never_touches_the_fallback(tmp_path: Path) -> None:
    codex, claude = ScriptedProvider(APPROVE), ScriptedProvider(APPROVE)
    worker, github, store = _worker(tmp_path, codex, claude)

    worker.tick()

    assert claude.runs == 0
    assert github.posted[0][2:4] == ("No findings.", "APPROVE")
    assert _only_job(store, JobStatus.POSTED).verdict == "APPROVE"


def test_a_primary_requesting_changes_is_an_answer_not_a_failure(tmp_path: Path) -> None:
    codex, claude = ScriptedProvider(FINDINGS), ScriptedProvider(APPROVE)
    worker, github, _ = _worker(tmp_path, codex, claude)

    worker.tick()

    assert claude.runs == 0
    assert github.posted[0][3] == "REQUEST_CHANGES"


def test_an_unavailable_primary_falls_back_at_once_and_says_so(tmp_path: Path) -> None:
    codex = ScriptedProvider(ProviderUnavailableError("You've hit your usage limit"))
    claude = ScriptedProvider(FINDINGS)
    worker, github, store = _worker(tmp_path, codex, claude)

    worker.tick()

    assert claude.runs == 1
    _, _, body, event, commit = github.posted[0]
    assert event == "REQUEST_CHANGES"
    assert commit == "head"
    assert "`claude`" in body and "`codex`" in body and "usage limit" in body
    assert body.endswith("`a.py:1` is wrong.")
    assert _only_job(store, JobStatus.POSTED).attempts == 1


def test_a_primary_that_cannot_even_start_falls_back(tmp_path: Path) -> None:
    codex = ScriptedProvider(start_error=ProviderUnavailableError("auth is not onboarded"))
    claude = ScriptedProvider(APPROVE)
    worker, github, _ = _worker(tmp_path, codex, claude)

    worker.tick()

    assert claude.runs == 1
    assert github.posted[0][3] == "APPROVE"
    assert "auth is not onboarded" in github.posted[0][2]


def test_a_transient_primary_failure_retries_the_primary_before_spending_credit(
    tmp_path: Path,
) -> None:
    codex = ScriptedProvider(RuntimeError("codex review timed out"), APPROVE)
    claude = ScriptedProvider(APPROVE)
    worker, github, store = _worker(tmp_path, codex, claude)

    worker.tick()

    assert claude.runs == 0
    assert github.posted == []
    assert _only_job(store, JobStatus.FAILED).attempts == 1

    worker.tick()

    assert claude.runs == 0
    assert codex.runs == 2
    assert github.posted[0][2] == "No findings."


def test_the_last_attempt_falls_back_on_any_failure(tmp_path: Path) -> None:
    codex = ScriptedProvider(RuntimeError("boom 1"), RuntimeError("boom 2"), RuntimeError("boom 3"))
    claude = ScriptedProvider(APPROVE)
    worker, github, _ = _worker(tmp_path, codex, claude, max_attempts=3)

    worker.tick()
    worker.tick()
    assert claude.runs == 0

    worker.tick()

    assert claude.runs == 1
    assert codex.runs == 3
    assert "boom 3" in github.posted[0][2]


def test_no_fallback_configured_keeps_the_single_provider_behaviour(tmp_path: Path) -> None:
    codex = ScriptedProvider(ProviderUnavailableError("You've hit your usage limit"))
    claude = ScriptedProvider(APPROVE)
    worker, github, store = _worker(tmp_path, codex, claude, fallbacks="[]")

    worker.tick()

    assert claude.runs == 0
    assert github.posted == []
    assert "usage limit" in (_only_job(store, JobStatus.FAILED).last_error or "")


def test_a_chain_with_nobody_left_names_every_provider_and_posts_nothing(tmp_path: Path) -> None:
    codex = ScriptedProvider(ProviderUnavailableError("codex quota"))
    claude = ScriptedProvider(ProviderUnavailableError("claude credit gone"))
    worker, github, store = _worker(tmp_path, codex, claude)

    worker.tick()

    assert github.posted == []
    error = _only_job(store, JobStatus.FAILED).last_error or ""
    assert "no review provider could run" in error
    assert "codex: codex quota" in error
    assert "claude: claude credit gone" in error


def test_every_session_that_started_is_cleaned_up(tmp_path: Path) -> None:
    codex = ScriptedProvider(ProviderUnavailableError("quota"))
    claude = ScriptedProvider(APPROVE)
    worker, _, _ = _worker(tmp_path, codex, claude)

    worker.tick()

    assert len(codex.cleaned) == 1
    assert len(claude.cleaned) == 1


def test_a_long_failure_reason_stays_out_of_the_review_body(tmp_path: Path) -> None:
    codex = ScriptedProvider(ProviderUnavailableError("limit reached\n" + "x" * 5_000))
    claude = ScriptedProvider(APPROVE)
    worker, github, _ = _worker(tmp_path, codex, claude)

    worker.tick()

    body = github.posted[0][2]
    assert "limit reached" in body
    assert "xxx" not in body


def test_run_pr_review_uses_the_repo_chain(tmp_path: Path) -> None:
    codex = ScriptedProvider(ProviderUnavailableError("quota"))
    claude = ScriptedProvider(APPROVE)
    worker, github, _ = _worker(tmp_path, codex, claude)

    summary = worker.run_pr_review("alvaro-francisco-gil/homelab", 1)

    assert summary.verdict == "APPROVE"
    assert claude.runs == 1
    assert github.posted[0][3] == "APPROVE"
