from __future__ import annotations

import logging
import os
from pathlib import Path

import docker
import uvicorn

from .api import create_app
from .config import ReviewConfig
from .git_worktree import GitWorktreeManager
from .github_client import GitHubClient
from .job_store import ReviewJobStore
from .providers.claude import ClaudeReviewProvider
from .providers.codex import CodexReviewProvider
from .worker import ReviewWorker


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config_path = Path(os.environ.get("AGENT_REVIEW_CONFIG", "/etc/github-review/agent-review.yml"))
    state_dir = Path(os.environ.get("STATE_DIR", "/var/lib/github-review"))
    projects_root = Path(os.environ.get("PROJECTS_ROOT", "/opt/personal/projects"))
    router_url = os.environ.get("ROUTER_INTERNAL_URL", "http://claude-router:8000")
    codex_state_root = Path(os.environ.get("CODEX_STATE_ROOT", "/opt/personal/codex-code-state"))
    codex_model = os.environ.get("CODEX_MODEL", "gpt-5.5")
    claude_api_key_file = Path(
        os.environ.get("CLAUDE_REVIEW_API_KEY_FILE", "/opt/personal/claude-review-state/api-key")
    )
    claude_model = os.environ.get("CLAUDE_REVIEW_MODEL", "claude-sonnet-5-5")
    claude_max_budget_usd = float(os.environ.get("CLAUDE_REVIEW_MAX_BUDGET_USD", "3"))
    reviewer_bot = os.environ.get("REVIEWER_BOT_LOGIN", "homelab-claude-reviewer[bot]")
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "900"))
    max_attempts = int(os.environ.get("MAX_REVIEW_ATTEMPTS", "3"))
    max_rounds = int(os.environ.get("MAX_REVIEW_ROUNDS", "5"))

    cfg = ReviewConfig.load(config_path)
    store = ReviewJobStore(state_dir / "jobs.db")
    store.init()
    github = GitHubClient(router_url=router_url, github_role="reviewer")
    docker_client = docker.from_env()
    worktrees = GitWorktreeManager(
        projects_root=projects_root,
        # Container runs as root; drop git to the operator uid (the first login, 1000)
        # so objects in the shared clone stay operator-owned for the deploy.
        run_as_uid=int(os.environ.get("WORKTREE_RUN_AS_UID", "1000")),
        run_as_gid=int(os.environ.get("WORKTREE_RUN_AS_GID", "1000")),
    )
    claude_provider = ClaudeReviewProvider(
        docker_client=docker_client,
        worktrees=worktrees,
        projects_root=projects_root,
        api_key_file=claude_api_key_file,
        model=claude_model,
        max_budget_usd=claude_max_budget_usd,
    )
    claude_provider.reap_orphans()
    worker = ReviewWorker(
        config=cfg,
        store=store,
        github=github,
        worktrees=worktrees,
        providers={
            "codex": CodexReviewProvider(
                docker_client=docker_client,
                projects_root=projects_root,
                codex_state_root=codex_state_root,
                router_url=router_url,
                model=codex_model,
            ),
            "claude": claude_provider,
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
