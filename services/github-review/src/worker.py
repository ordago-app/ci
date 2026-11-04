from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .config import RepoPolicy, ReviewConfig, ToolProfile
from .job_store import JobStatus, ReviewJob, ReviewJobStore
from .poller import ReviewPoller
from .prompt import build_review_prompt
from .provider import (
    ProviderUnavailableError,
    ReviewLimitExceededError,
    ReviewProvider,
    ReviewResult,
)

# Opens the body of a carried approval. pr-land (agent-skills) matches it to keep
# these out of its round count, so the two strings must stay identical.
CARRIED_REVIEW_MARKER = "<!-- ai-review:carried-approval -->"

_MAX_REASON_CHARS = 200


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
            before = self._worktrees.diff_fingerprint(
                repo_dir=repo_dir, base_sha=previous.base_sha, head_sha=previous.head_sha
            )
            after = self._worktrees.diff_fingerprint(
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
                f"Approval carried over from `{source[:12]}`: this head changes exactly the "
                f"same files to exactly the same contents (diff fingerprint {before[:12]}), "
                "so no new review ran. Only the base moved underneath it, and CI re-verifies "
                "that.",
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
        try:
            pr = self._github.get_pull_request(job.repo, job.pr_number)
            if pr.head_sha != job.head_sha:
                self._store.mark_skipped(job.id, f"head moved to {pr.head_sha}")
                return
            prompt = build_review_prompt(
                job=job,
                pr=pr,
                changed_files=self._github.changed_files(job.repo, job.pr_number),
                diffstat=self._github.diffstat(job.repo, job.pr_number),
                ci_summary=self._github.ci_summary(job.repo, job.head_sha)
                if repo_policy.run_ci_first
                else "not requested",
            )
            result = self._review_with_chain(job, repo_policy, profile, prompt)
            self._github.post_review(
                job.repo, job.pr_number, result.body, result.event, commit_id=job.head_sha
            )
            self._store.mark_posted(job.id, verdict=result.event)
        except ReviewLimitExceededError as exc:
            self._store.mark_given_up(job.id, str(exc), max_attempts=self._max_attempts)
            self._report_failure(job, exc)
        except Exception as exc:
            self._store.mark_failed(job.id, str(exc))
            self._report_failure(job, exc)

    def _review_with_chain(
        self,
        job: ReviewJob,
        repo_policy: RepoPolicy,
        profile: ToolProfile,
        prompt: str,
    ) -> ReviewResult:
        """The first answer from the repo's chain. An answer is any verdict: a fallback
        never runs because the primary disagreed.

        A fallback spends real credit and a later tick's retry does not, so only two
        things move down the chain: a provider that says it cannot run, and any failure
        on the job's last attempt. Anything else re-raises for the normal retry, which
        starts again from the primary.

        Each provider gets its own worktree: a codex session mounts its read-write, so
        the next provider must not read what a failed one left behind."""
        last_attempt = job.attempts + 1 >= self._max_attempts
        chain = [job.provider, *repo_policy.fallback_providers]
        failures: list[tuple[str, Exception]] = []
        for index, name in enumerate(chain):
            provider = self._providers[name]
            worktree = self._worktrees.prepare(
                repo_dir=self._projects_root / job.project / "repo",
                project=job.project,
                pr_number=job.pr_number,
                head_sha=job.head_sha,
                # The head is already in the clone by then, so a fetch failing now must
                # not stop the fallback the chain exists to reach.
                fetch=index == 0,
            )
            session = None
            try:
                session = provider.start_review_session(job, worktree, profile)
                result = provider.run_review(
                    session, prompt, timeout_seconds=profile.max_runtime_minutes * 60
                )
            except ReviewLimitExceededError:
                raise
            except ProviderUnavailableError as exc:
                failures.append((name, exc))
                continue
            except Exception as exc:
                if not last_attempt:
                    raise
                failures.append((name, exc))
                continue
            finally:
                if session is not None:
                    provider.cleanup(session)
                self._discard_worktree(job, worktree)
            return self._attributed(job, name, failures, result)
        if len(failures) == 1:
            raise failures[0][1]
        raise RuntimeError(
            "no review provider could run: " + "; ".join(f"{name}: {exc}" for name, exc in failures)
        )

    def _discard_worktree(self, job: ReviewJob, worktree: Path) -> None:
        """Log rather than raise: this runs after a provider answered, and a directory
        that will not delete must not cost that review. `prepare` clears the path anyway."""
        try:
            self._worktrees.cleanup(worktree)
        except Exception as exc:
            print(
                f"[github-review] could not remove worktree {worktree} for "
                f"{job.repo}#{job.pr_number}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    def _attributed(
        self,
        job: ReviewJob,
        reviewer: str,
        failures: list[tuple[str, Exception]],
        result: ReviewResult,
    ) -> ReviewResult:
        """Say so when a fallback wrote the review: a different model read this diff,
        and the credit it spent is the operator's to notice."""
        if not failures:
            return result
        reasons = "; ".join(f"`{name}`: {_short_reason(exc)}" for name, exc in failures)
        print(
            f"[github-review] {job.repo}#{job.pr_number}@{job.head_sha[:12]} reviewed by "
            f"fallback {reviewer} — {reasons}",
            file=sys.stderr,
            flush=True,
        )
        note = f"> Reviewed by fallback `{reviewer}` because of {reasons}"
        return ReviewResult(body=f"{note}\n\n{result.body}", event=result.event)

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

    def _policy_for_repo(self, repo: str) -> RepoPolicy:
        for repo_policy in self._config.enabled_repos():
            if repo_policy.repo == repo:
                return repo_policy
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

        repo_policy = self._policy_for_repo(repo)
        job = self._store.enqueue(
            repo, repo_policy.project, repo_policy.provider, pr_number, head_sha, pr.base_sha
        )
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


def _short_reason(exc: Exception) -> str:
    lines = str(exc).strip().splitlines()
    return (lines[0] if lines else type(exc).__name__)[:_MAX_REASON_CHARS]


@dataclass(frozen=True)
class ReviewResultSummary:
    head_sha: str
    verdict: str
    escalated: bool
