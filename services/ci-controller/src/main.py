from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

import uvicorn

from src.api import create_app
from src.config import ControllerConfig
from src.controller import Controller
from src.docker_adapter import DockerPool
from src.github_adapter import GitHubAdapter
from src.host_stats import read_host_stats
from src.ledger import Ledger
from src.metrics import MetricsStore
from src.scheduler import LocalScheduler, Scheduler
from src.scheduler_client import HttpScheduler


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def build_scheduler(config: ControllerConfig, scheduler_url: str | None) -> Scheduler:
    if scheduler_url:
        return HttpScheduler(scheduler_url)
    return LocalScheduler(config=config, ledger=Ledger())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config_path = Path(
        os.environ.get("CI_CONTROLLER_CONFIG", "/etc/ci-controller/ci-controller.yml")
    )
    poll_interval = float(os.environ.get("POLL_INTERVAL_SECONDS", "15"))
    host = os.environ.get("RUNNER_HOST", "powerserver")

    config = ControllerConfig.load(config_path)
    if host not in config.resolved_hosts():
        raise SystemExit(f"RUNNER_HOST '{host}' is not a configured host")

    scheduler = build_scheduler(config, os.environ.get("CI_SCHEDULER_URL"))

    private_key = base64.b64decode(_require("GITHUB_RUNNER_APP_PRIVATE_KEY_B64")).decode()
    github = GitHubAdapter(
        app_id=_require("GITHUB_RUNNER_APP_ID"),
        private_key_pem=private_key,
    )
    # One DockerAdapter per configured host, each dialing its own docker_endpoint
    # (resolved_hosts() falls back to DOCKER_PROXY_URL when `hosts:` is absent, so
    # a single-host deploy stays wired exactly as before per-host config existed).
    docker_pool = DockerPool(config)

    metrics = MetricsStore(os.environ.get("CI_CONTROLLER_DB", "/var/lib/ci-controller/metrics.db"))
    controller = Controller(
        config=config,
        github=github,
        docker=docker_pool,
        scheduler=scheduler,
        metrics=metrics,
        host_stats_reader=read_host_stats,
        host=host,
    )
    app = create_app(controller, poll_interval=poll_interval)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
