from __future__ import annotations

import os
from pathlib import Path

import docker
import uvicorn

from .api import create_app
from .config import ReviewConfig
from .git_worktree import GitWorktreeManager
from .github_client import GitHubClient
from .job_store import ReviewJobStore
from .providers.codex import CodexReviewProvider
from .worker import ReviewWorker


def main() -> None:
    config_path = Path(os.environ.get("AGENT_REVIEW_CONFIG", "/etc/github-review/agent-review.yml"))
    state_dir = Path(os.environ.get("STATE_DIR", "/var/lib/github-review"))
    projects_root = Path(os.environ.get("PROJECTS_ROOT", "/opt/personal/projects"))
    router_url = os.environ.get("ROUTER_INTERNAL_URL", "http://claude-router:8000")
    codex_state_root = Path(os.environ.get("CODEX_STATE_ROOT", "/opt/personal/codex-code-state"))
    codex_model = os.environ.get("CODEX_MODEL", "gpt-5.5")
    reviewer_bot = os.environ.get("REVIEWER_BOT_LOGIN", "homelab-claude-reviewer[bot]")
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
    max_attempts = int(os.environ.get("MAX_REVIEW_ATTEMPTS", "3"))
    max_rounds = int(os.environ.get("MAX_REVIEW_ROUNDS", "5"))

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
        max_attempts=max_attempts,
        max_rounds=max_rounds,
    )

    app = create_app(worker, poll_interval=poll_interval)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
