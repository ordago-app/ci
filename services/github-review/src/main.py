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
from .config import ClaudeAccount, CodexAccount, ConfigError, ReviewConfig
from .git_worktree import GitWorktreeManager
from .github_app import GitHubAppTokens
from .github_client import GitHubClient
from .job_store import ReviewJobStore
from .provider import ReviewProvider
from .providers._agent_container import reap_review_containers
from .providers.claude import ClaudeReviewProvider
from .providers.codex import CodexReviewProvider
from .worker import ReviewWorker


def required_env(environ: Mapping[str, str], name: str) -> str:
    """No default for anything that differs between hosts or instances: a
    default here would be one operator's machine wearing a neutral name."""
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} must be set (see compose.yml)")
    return value


def build_providers(
    config: ReviewConfig,
    *,
    environ: Mapping[str, str],
    docker_client: DockerClient,
    worktrees: GitWorktreeManager,
    projects_root: Path,
) -> dict[str, ReviewProvider]:
    """One provider per declared account, keyed by the name chains use.

    Every account is built whether or not its credential is present now: a
    chain naming one with no credential fails over past it at review time with
    "no credential", which says what to fix, instead of the service refusing to
    start because one spare account is unset."""
    claude_image = required_env(environ, "CLAUDE_REVIEW_IMAGE")
    codex_image = required_env(environ, "CODEX_REVIEW_IMAGE")
    providers: dict[str, ReviewProvider] = {}
    for name, account in config.accounts.items():
        if isinstance(account, ClaudeAccount):
            providers[name] = ClaudeReviewProvider(
                account=name,
                settings=account,
                docker_client=docker_client,
                worktrees=worktrees,
                projects_root=projects_root,
                image=claude_image,
                instance=config.instance,
                environ=environ,
            )
        elif isinstance(account, CodexAccount):
            providers[name] = CodexReviewProvider(
                account=name,
                settings=account,
                docker_client=docker_client,
                projects_root=projects_root,
                image=codex_image,
                instance=config.instance,
            )
        if not account.credential.read(environ):
            logging.warning(
                "account %s has no credential in %s; chains naming it will fail over past it",
                name,
                account.credential.describe(),
            )
    return providers


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    environ = os.environ
    config = ReviewConfig.load(Path(required_env(environ, "AGENT_REVIEW_CONFIG")))
    state_dir = Path(required_env(environ, "STATE_DIR"))
    projects_root = Path(required_env(environ, "PROJECTS_ROOT"))

    # The one credential the service cannot run without: unlike a review
    # account, there is nothing to fail over to.
    private_key = config.github_app.private_key.read(environ)
    if not private_key:
        raise ConfigError(
            f"github_app.private_key: nothing in {config.github_app.private_key.describe()}"
        )
    tokens = GitHubAppTokens(app_id=config.github_app.app_id, private_key=private_key)

    docker_client = docker.from_env()
    # Before any runner starts, so nothing live is mistaken for a leftover.
    reap_review_containers(docker_client, config.instance)
    worktrees = GitWorktreeManager(
        projects_root=projects_root,
        # The container runs as root; git drops to this uid so the clones it
        # writes stay owned by the host user who owns the data directory.
        run_as_uid=int(required_env(environ, "WORKTREE_RUN_AS_UID")),
        run_as_gid=int(required_env(environ, "WORKTREE_RUN_AS_GID")),
        tokens=tokens,
    )
    providers = build_providers(
        config,
        environ=environ,
        docker_client=docker_client,
        worktrees=worktrees,
        projects_root=projects_root,
    )
    stop = threading.Event()
    for provider in providers.values():
        # Each codex account's only login refresher (codex_auth.py).
        if isinstance(provider, CodexReviewProvider):
            provider.start_auth_keeper(stop)

    store = ReviewJobStore(state_dir / "jobs.db")
    store.init()
    limits = config.limits
    worker = ReviewWorker(
        config=config,
        store=store,
        github=GitHubClient(tokens=tokens),
        worktrees=worktrees,
        providers=providers,
        projects_root=projects_root,
        reviewer_bot=config.reviewer_bot,
        max_attempts=limits.max_attempts,
        max_rounds=limits.max_rounds,
        max_runs=limits.max_runs,
        retry_backoff_seconds=limits.retry_backoff_seconds,
        concurrency=limits.concurrency,
        max_restart_refunds=limits.max_restart_refunds,
        stuck_running_seconds=limits.stuck_running_seconds,
    )

    app = create_app(worker, poll_interval=limits.poll_interval_seconds)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
