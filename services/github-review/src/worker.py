from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .config import ReviewConfig
from .job_store import JobStatus, ReviewJob, ReviewJobStore
from .poller import ReviewPoller
from .prompt import build_review_prompt
from .provider import ReviewProvider

# Opens the body of a carried approval. pr-land (agent-skills) matches it to keep
# these out of its round count, so the two strings must stay identical.
CARRIED_REVIEW_MARKER = "<!-- ai-review:carried-approval -->"


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

    @property
    def reviewer_bot(self) -> str:
        return self._reviewer_bot

    @property
    def max_attempts(self) -> int:
        """Read by /status to tell a retryable failure from one that is out of retries."""
        return self._max_attempts

    @property
    def store(self) -> ReviewJobStore:
        """Read-only access for /status. Public so the API doesn't reach into _store."""
        return self._store

    def tick(self) -> None:
        ReviewPoller(
            self._config, self._store, self._github, reviewer_bot=self._reviewer_bot
        ).poll_once()
        for job in self._store.list_retryable(self._max_attempts):
            if self._carry_approval(job):
                continue
            if self._store.rounds_for(job.repo, job.pr_number) >= self._max_rounds:
                self._store.mark_skipped(job.id, f"max review rounds ({self._max_rounds}) reached")
                continue
            self._run_job(job)

    def _carry_approval(self, job: ReviewJob) -> bool:
        """Re-post the last APPROVE onto `job`'s head when the PR's own diff is unchanged.

        A rebase moves the base under a PR without changing what the PR changes, yet
        every new head SHA used to buy a full review run and a round. The review is
        a judgement of the diff; an identical diff has already been judged. What a
        rebase CAN break — the integration with the moved base — is CI's question,
        and CI re-runs on the new head regardless.

        Only the LATEST posted review may be carried, and only if it approved: an
        older approval followed by findings is not an approval of this PR any more.
        Anything that stops the comparison (a gc'd head, a git error) falls through
        to a normal review — the expensive answer, never a wrong one."""
        previous = self._store.latest_posted_before(job.repo, job.pr_number, job.head_sha)
        if previous is None or previous.verdict != "APPROVE":
            return False
        repo_dir = self._projects_root / job.project / "repo"
        try:
            self._worktrees.fetch(repo_dir)
            before = self._worktrees.diff_patch_id(
                repo_dir=repo_dir, base_sha=previous.base_sha, head_sha=previous.head_sha
            )
            after = self._worktrees.diff_patch_id(
                repo_dir=repo_dir, base_sha=job.base_sha, head_sha=job.head_sha
            )
            if before is None or before != after:
                return False
            if self._github.get_pull_request(job.repo, job.pr_number).head_sha != job.head_sha:
                return False
            source = previous.carried_from or previous.head_sha
            self._github.post_review(
                job.repo,
                job.pr_number,
                f"{CARRIED_REVIEW_MARKER}\n"
                f"Approval carried over from `{source[:12]}`: this head changes exactly what "
                f"that one did (`git patch-id` {before[:12]}), so no new review ran. "
                "Only the base moved underneath it, and CI re-verifies that.",
                "APPROVE",
                commit_id=job.head_sha,
            )
        except Exception as exc:
            print(
                f"[github-review] approval carry-over skipped for "
                f"{job.repo}#{job.pr_number}@{job.head_sha[:12]}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return False
        self._store.mark_carried(job.id, source)
        return True

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
            self._report_failure(job, exc)
        finally:
            if session is not None:
                self._providers[job.provider].cleanup(session)
            if worktree is not None:
                self._worktrees.cleanup(worktree)

    def _report_failure(self, job: ReviewJob, exc: Exception) -> None:
        """Put the failure somewhere a human actually looks.

        Recording it in last_error alone is a silent fallback: the reviewer failed
        every ordago-apps job from 2026-06-21 on a `git worktree add … Permission
        denied` and posted nothing, and `docker logs github-review` stayed clean
        the whole time. Nobody saw it for seven weeks.
        """
        streak = self._store.consecutive_failures(job.repo)
        detail = f"{job.repo}#{job.pr_number}@{job.head_sha[:12]} (job {job.id}): {exc}"
        if streak > 1:
            # Distinct wording so a chronically broken repo is greppable on its
            # own — the one-off case must not drown it.
            print(
                f"[github-review] review FAILED — {detail}; "
                f"{streak} consecutive failures for {job.repo} with no review posted since",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(f"[github-review] review FAILED — {detail}", file=sys.stderr, flush=True)

    def _project_for_repo(self, repo: str) -> str:
        for repo_policy in self._config.enabled_repos():
            if repo_policy.repo == repo:
                return repo_policy.project
        raise KeyError(f"No enabled repo policy configured for {repo!r}")

    def run_pr_review(self, repo: str, pr_number: int) -> ReviewResultSummary:
        pr = self._github.get_pull_request(repo, pr_number)
        head_sha = pr.head_sha

        # Idempotent: a posted row means a review already exists for this head.
        # Legacy rows migrated from the old DB have verdict NULL; treat them as
        # already-reviewed (REQUEST_CHANGES) rather than posting a duplicate.
        existing = self._store.get_posted(repo, pr_number, head_sha)
        if existing is not None:
            return ReviewResultSummary(head_sha, existing.verdict or "REQUEST_CHANGES", False)

        project = self._project_for_repo(repo)
        job = self._store.enqueue(repo, project, "codex", pr_number, head_sha, pr.base_sha)
        # Before the cap: a carried approval costs nothing and is not a round.
        if self._carry_approval(job):
            return ReviewResultSummary(head_sha, "APPROVE", False)

        # Cost backstop: refuse beyond the hard round cap.
        if self._store.rounds_for(repo, pr_number) >= self._max_rounds:
            self._store.mark_skipped(job.id, f"max review rounds ({self._max_rounds}) reached")
            return ReviewResultSummary(head_sha, "REQUEST_CHANGES", True)

        self._run_job(job)
        done = self._store.get(job.id)
        if done is None or done.status != JobStatus.POSTED or done.verdict is None:
            detail = (
                done.last_error
                if done is not None and done.last_error
                else f"status={done.status if done is not None else 'missing'}"
            )
            raise RuntimeError(
                f"review did not complete for {repo}#{pr_number}@{head_sha}: {detail}"
            )
        return ReviewResultSummary(head_sha, done.verdict, False)


@dataclass(frozen=True)
class ReviewResultSummary:
    head_sha: str
    verdict: str
    escalated: bool
