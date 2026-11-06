from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from pathlib import Path

import docker
import uvicorn
from docker import DockerClient

from .api import create_app
from .config import ReviewConfig
from .git_worktree import GitWorktreeManager
from .github_client import GitHubClient
from .job_store import ReviewJobStore
from .provider import ReviewProvider
from .providers.claude import ClaudeCredentialEnv, ClaudeReviewProvider
from .providers.codex import CodexReviewProvider
from .worker import ReviewWorker


def build_claude_providers(
    env: Mapping[str, str],
    *,
    docker_client: DockerClient,
    projects_root: Path,
    router_url: str,
    model: str,
) -> dict[str, ClaudeReviewProvider]:
    # One provider per Claude account. They hold independent quotas, which is
    # the entire reason to wire more than one: when one is spent, the next is
    # still there. Registered under the account name so agent-review.yml orders
    # them by account rather than by an opaque "primary"/"secondary" that reads
    # backwards the day the accounts swap.
    claude_accounts: dict[str, tuple[ClaudeCredentialEnv, str]] = {
        "claude-work": ("CLAUDE_CODE_OAUTH_TOKEN", env.get("CLAUDE_CODE_OAUTH_TOKEN", "")),
        "claude-personal": (
            "CLAUDE_CODE_OAUTH_TOKEN",
            env.get("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", ""),
        ),
        "claude-api": ("ANTHROPIC_API_KEY", env.get("CLAUDE_REVIEW_API_KEY", "")),
    }
    providers: dict[str, ClaudeReviewProvider] = {}
    for account, (credential_env, credential) in claude_accounts.items():
        if not credential:
            # Registered anyway, so a chain naming it fails loudly at review
            # time with "no credential" rather than at config load with
            # "unknown provider" — the first says what to fix.
            logging.warning(
                "no credential for %s; chains naming it will fail over past it", account
            )
        providers[account] = ClaudeReviewProvider(
            account=account,
            credential_env=credential_env,
            credential=credential,
            docker_client=docker_client,
            projects_root=projects_root,
            router_url=router_url,
            model=model,
        )
    return providers


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config_path = Path(os.environ.get("AGENT_REVIEW_CONFIG", "/etc/github-review/agent-review.yml"))
    state_dir = Path(os.environ.get("STATE_DIR", "/var/lib/github-review"))
    projects_root = Path(os.environ.get("PROJECTS_ROOT", "/opt/personal/projects"))
    router_url = os.environ.get("ROUTER_INTERNAL_URL", "http://claude-router:8000")
    codex_state_root = Path(os.environ.get("CODEX_STATE_ROOT", "/opt/personal/codex-code-state"))
    codex_model = os.environ.get("CODEX_MODEL", "")
    reviewer_bot = os.environ.get("REVIEWER_BOT_LOGIN", "homelab-claude-reviewer[bot]")
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
    max_attempts = int(os.environ.get("MAX_REVIEW_ATTEMPTS", "3"))
    max_rounds = int(os.environ.get("MAX_REVIEW_ROUNDS", "5"))
    max_runs = int(os.environ.get("MAX_REVIEW_RUNS", "10"))
    retry_backoff = int(os.environ.get("RETRY_BACKOFF_SECONDS", "900"))
    max_restart_refunds = int(os.environ.get("MAX_RESTART_REFUNDS", "2"))
    stuck_running = int(os.environ.get("STUCK_RUNNING_SECONDS", "10800"))
    concurrency = int(os.environ.get("REVIEW_CONCURRENCY", "3"))
    claude_model = os.environ.get("CLAUDE_REVIEW_MODEL", "claude-opus-5")

    docker_client = docker.from_env()
    codex = CodexReviewProvider(
        docker_client=docker_client,
        projects_root=projects_root,
        codex_state_root=codex_state_root,
        router_url=router_url,
        model=codex_model,
    )
    # The host's only Codex login refresher: every other Codex consumer copies the
    # shared login and relies on it staying fresh (codex_auth.py).
    codex.start_auth_keeper(threading.Event())
    providers: dict[str, ReviewProvider] = {"codex": codex}
    providers.update(
        build_claude_providers(
            os.environ,
            docker_client=docker_client,
            projects_root=projects_root,
            router_url=router_url,
            model=claude_model,
        )
    )

    cfg = ReviewConfig.load(config_path, known_providers=providers.keys())
    store = ReviewJobStore(state_dir / "jobs.db")
    store.init()
    github = GitHubClient(router_url=router_url, github_role="reviewer")
    worker = ReviewWorker(
        config=cfg,
        store=store,
        github=github,
        worktrees=GitWorktreeManager(
            projects_root=projects_root,
            # Container runs as root; drop git to the operator uid (powerbot=1000)
            # so objects in the shared clone stay operator-owned for the deploy.
            run_as_uid=int(os.environ.get("WORKTREE_RUN_AS_UID", "1000")),
            run_as_gid=int(os.environ.get("WORKTREE_RUN_AS_GID", "1000")),
        ),
        providers=providers,
        projects_root=projects_root,
        reviewer_bot=reviewer_bot,
        max_attempts=max_attempts,
        max_rounds=max_rounds,
        max_runs=max_runs,
        retry_backoff_seconds=retry_backoff,
        concurrency=concurrency,
        max_restart_refunds=max_restart_refunds,
        stuck_running_seconds=stuck_running,
    )

    app = create_app(worker, poll_interval=poll_interval)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
