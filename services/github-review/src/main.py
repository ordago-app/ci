from __future__ import annotations

import os
import time
from pathlib import Path

import docker

from .config import ReviewConfig
from .git_worktree import GitWorktreeManager
from .github_client import GitHubClient
from .job_store import JobStatus, ReviewJob, ReviewJobStore
from .poller import ReviewPoller
from .prompt import build_review_prompt
from .provider import ReviewProvider
from .providers.codex import CodexReviewProvider


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
    ) -> None:
        self._config = config
        self._store = store
        self._github = github
        self._worktrees = worktrees
        self._providers = providers
        self._projects_root = projects_root
        self._reviewer_bot = reviewer_bot

    def tick(self) -> None:
        ReviewPoller(
            self._config, self._store, self._github, reviewer_bot=self._reviewer_bot
        ).poll_once()
        for job in self._store.list_by_status(JobStatus.QUEUED):
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
            self._github.post_review(job.repo, job.pr_number, result.body, result.event)
            self._store.mark_posted(job.id)
        except Exception as exc:
            self._store.mark_failed(job.id, str(exc))
        finally:
            if session is not None:
                self._providers[job.provider].cleanup(session)
            if worktree is not None:
                self._worktrees.cleanup(worktree)


def main() -> None:
    config_path = Path(os.environ.get("AGENT_REVIEW_CONFIG", "/etc/github-review/agent-review.yml"))
    state_dir = Path(os.environ.get("STATE_DIR", "/var/lib/github-review"))
    projects_root = Path(os.environ.get("PROJECTS_ROOT", "/opt/personal/projects"))
    router_url = os.environ.get("ROUTER_INTERNAL_URL", "http://claude-router:8000")
    codex_state_root = Path(os.environ.get("CODEX_STATE_ROOT", "/opt/personal/codex-code-state"))
    codex_model = os.environ.get("CODEX_MODEL", "gpt-5.5")
    reviewer_bot = os.environ.get("REVIEWER_BOT_LOGIN", "homelab-claude-reviewer[bot]")
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))

    cfg = ReviewConfig.load(config_path)
    store = ReviewJobStore(state_dir / "jobs.db")
    store.init()
    github = GitHubClient(router_url=router_url, github_role="reviewer")
    worker = ReviewWorker(
        config=cfg,
        store=store,
        github=github,
        worktrees=GitWorktreeManager(projects_root=projects_root),
        providers={
            "codex": CodexReviewProvider(
                docker_client=docker.from_env(),
                projects_root=projects_root,
                codex_state_root=codex_state_root,
                router_url=router_url,
                model=codex_model,
            )
        },
        projects_root=projects_root,
        reviewer_bot=reviewer_bot,
    )

    while True:
        worker.tick()
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
