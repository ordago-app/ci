from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from .config import ReviewConfig
from .github_client import DEPENDENCY_BOTS, PullRequest
from .job_store import JobStatus, ReviewJob, ReviewJobStore

REVIEW_LABEL = "ai-review"

# A head's CI verdict is re-read at most this often. Longer than the 60 s poll,
# so a long queue costs one check-runs call per head every other poll.
CI_STATUS_TTL_SECONDS = 120.0

_log = logging.getLogger(__name__)


class PullRequestSource(Protocol):
    def list_open_prs(self, repo: str) -> list[PullRequest]: ...


class CheckRunSource(Protocol):
    def head_ci_green(self, repo: str, head_sha: str) -> bool: ...


class CiGreenCache:
    """Whether a head's CI is all green, read through the reviewer's token.

    Only ever a priority hint: an unreadable answer (403 until the reviewer App
    has Checks: Read, a 404, a network error) reads as not green, so the queue
    falls back to PR age. That is logged once per process, not once per head
    per poll, and kept for the TTL like any other answer so a broken endpoint
    is not hammered."""

    def __init__(
        self,
        github: CheckRunSource,
        *,
        ttl_seconds: float = CI_STATUS_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._github = github
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[bool, float]] = {}
        self._warned = False

    def is_green(self, repo: str, head_sha: str) -> bool:
        now = self._clock()
        self._cache = {k: v for k, v in self._cache.items() if now - v[1] < self._ttl}
        cached = self._cache.get((repo, head_sha))
        if cached is not None:
            return cached[0]
        try:
            green = self._github.head_ci_green(repo, head_sha)
        except Exception as exc:
            if not self._warned:
                self._warned = True
                _log.warning(
                    "cannot read check runs (%s: %s); review queue falls back to PR age "
                    "order. Logged once per process.",
                    type(exc).__name__,
                    exc,
                )
            green = False
        self._cache[(repo, head_sha)] = (green, now)
        return green


class ReviewPoller:
    def __init__(
        self,
        config: ReviewConfig,
        store: ReviewJobStore,
        github: PullRequestSource,
        *,
        reviewer_bot: str,
        ci: CiGreenCache | None = None,
    ) -> None:
        """`ci` None leaves every head's `ci_green` as it is: age order only."""
        self._config = config
        self._store = store
        self._github = github
        self._reviewer_bot = reviewer_bot
        self._ci = ci

    def poll_once(self) -> list[ReviewJob]:
        created: list[ReviewJob] = []
        for repo_policy in self._config.enabled_repos():
            for pr in self._github.list_open_prs(repo_policy.repo):
                if pr.state != "open":
                    continue
                if pr.draft and not repo_policy.review_drafts:
                    continue
                if pr.author == self._reviewer_bot:
                    continue
                # A bot PR is reviewed only when it is a dependency bump and the
                # repo opts in; agent and other bot PRs never are.
                if pr.author.endswith("[bot]") and not (
                    repo_policy.review_dependency_bots and pr.author in DEPENDENCY_BOTS
                ):
                    continue
                if repo_policy.review_mode == "labeled" and REVIEW_LABEL not in pr.labels:
                    continue
                job = self._store.enqueue(
                    repo=repo_policy.repo,
                    project=repo_policy.project,
                    provider=repo_policy.providers[0],
                    pr_number=pr.number,
                    head_sha=pr.head_sha,
                    base_sha=pr.base_sha,
                )
                job = self._refresh_ci_green(job)
                created.append(job)
        return created

    def _refresh_ci_green(self, job: ReviewJob) -> ReviewJob:
        """Only a head still waiting for a runner is worth a check-runs call."""
        if self._ci is None or job.status not in (JobStatus.QUEUED, JobStatus.FAILED):
            return job
        green = self._ci.is_green(job.repo, job.head_sha)
        if green == job.ci_green:
            return job
        self._store.set_ci_green(job.id, green)
        return replace(job, ci_green=green)
