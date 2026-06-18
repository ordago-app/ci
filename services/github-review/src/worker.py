from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import ReviewConfig
from .job_store import ReviewJob, ReviewJobStore
from .poller import ReviewPoller
from .prompt import build_review_prompt
from .provider import ReviewProvider


class ReviewWorker:
    def __init__(
        self,
        *,
        config: ReviewConfig,
        store: ReviewJobStore,
        github,
        worktrees,
        providers: dict[str, ReviewProvider],
        projects_root: Path,
        reviewer_bot: str,
        max_attempts: int = 3,
        max_rounds: int = 5,
    ) -> None:
        self._config = config
        self._store = store
        self._github = github
        self._worktrees = worktrees
        self._providers = providers
        self._projects_root = projects_root
        self._reviewer_bot = reviewer_bot
        self._max_attempts = max_attempts
        self._max_rounds = max_rounds

    def tick(self) -> None:
        ReviewPoller(
            self._config, self._store, self._github, reviewer_bot=self._reviewer_bot
        ).poll_once()
        for job in self._store.list_retryable(self._max_attempts):
            self._run_job(job)

    def _run_job(self, job: ReviewJob) -> None:
        repo_policy = next(
            repo for repo in self._config.enabled_repos() if repo.project == job.project
        )
        profile = self._config.profile_for(repo_policy)
        self._store.mark_running(job.id)
        worktree = None
        session = None
        try:
            pr = self._github.get_pull_request(job.repo, job.pr_number)
            if pr.head_sha != job.head_sha:
                self._store.mark_skipped(job.id, f"head moved to {pr.head_sha}")
                return
            repo_dir = self._projects_root / job.project / "repo"
            worktree = self._worktrees.prepare(
                repo_dir=repo_dir,
                project=job.project,
                pr_number=job.pr_number,
                head_sha=job.head_sha,
            )
            prompt = build_review_prompt(
                job=job,
                pr=pr,
                changed_files=self._github.changed_files(job.repo, job.pr_number),
                diffstat=self._github.diffstat(job.repo, job.pr_number),
                ci_summary=self._github.ci_summary(job.repo, job.head_sha)
                if repo_policy.run_ci_first
                else "not requested",
            )
            provider = self._providers[job.provider]
            session = provider.start_review_session(job, worktree, profile)
            result = provider.run_review(
                session,
                prompt,
                timeout_seconds=profile.max_runtime_minutes * 60,
            )
            self._github.post_review(
                job.repo, job.pr_number, result.body, result.event, commit_id=job.head_sha
            )
            self._store.mark_posted(job.id, verdict=result.event)
        except Exception as exc:
            self._store.mark_failed(job.id, str(exc))
        finally:
            if session is not None:
                self._providers[job.provider].cleanup(session)
            if worktree is not None:
                self._worktrees.cleanup(worktree)

    def _project_for_repo(self, repo: str) -> str:
        for repo_policy in self._config.enabled_repos():
            if repo_policy.repo == repo:
                return repo_policy.project
        raise KeyError(f"No enabled repo policy configured for {repo!r}")

    def run_pr_review(self, repo: str, pr_number: int) -> ReviewResultSummary:
        pr = self._github.get_pull_request(repo, pr_number)
        head_sha = pr.head_sha

        # Idempotent: if this exact head was already reviewed, return its verdict.
        existing = self._store.get_posted(repo, pr_number, head_sha)
        if existing is not None and existing.verdict is not None:
            return ReviewResultSummary(head_sha, existing.verdict, False)

        # Cost backstop: refuse beyond the hard round cap.
        if self._store.rounds_for(repo, pr_number) >= self._max_rounds:
            return ReviewResultSummary(head_sha, "REQUEST_CHANGES", True)

        project = self._project_for_repo(repo)
        job = self._store.enqueue(repo, project, "codex", pr_number, head_sha, pr.base_sha)
        self._run_job(job)
        done = self._store.get(job.id)
        verdict = done.verdict if done and done.verdict else "REQUEST_CHANGES"
        return ReviewResultSummary(head_sha, verdict, False)


@dataclass(frozen=True)
class ReviewResultSummary:
    head_sha: str
    verdict: str
    escalated: bool
