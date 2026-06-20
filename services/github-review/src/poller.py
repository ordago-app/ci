from __future__ import annotations

from typing import Protocol

from .config import ReviewConfig
from .github_client import PullRequest
from .job_store import ReviewJob, ReviewJobStore

REVIEW_LABEL = "ai-review"


class PullRequestSource(Protocol):
    def list_open_prs(self, repo: str) -> list[PullRequest]: ...


class ReviewPoller:
    def __init__(
        self,
        config: ReviewConfig,
        store: ReviewJobStore,
        github: PullRequestSource,
        *,
        reviewer_bot: str,
    ) -> None:
        self._config = config
        self._store = store
        self._github = github
        self._reviewer_bot = reviewer_bot

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
                # Dependabot and other bot PRs are mechanical version bumps; CI
                # is the right gate, so skip *[bot] authors unless opted in.
                if not repo_policy.review_bots and pr.author.endswith("[bot]"):
                    continue
                if repo_policy.review_mode == "labeled" and REVIEW_LABEL not in pr.labels:
                    continue
                job = self._store.enqueue(
                    repo=repo_policy.repo,
                    project=repo_policy.project,
                    provider=repo_policy.provider,
                    pr_number=pr.number,
                    head_sha=pr.head_sha,
                    base_sha=pr.base_sha,
                )
                created.append(job)
        return created
