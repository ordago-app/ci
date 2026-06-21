from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

import docker
import uvicorn

from src.api import create_app
from src.config import ControllerConfig
from src.controller import Controller
from src.docker_adapter import DockerAdapter
from src.github_adapter import GitHubAdapter
from src.ledger import Ledger


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config_path = Path(
        os.environ.get("CI_CONTROLLER_CONFIG", "/etc/ci-controller/ci-controller.yml")
    )
    poll_interval = float(os.environ.get("POLL_INTERVAL_SECONDS", "15"))
    host = os.environ.get("RUNNER_HOST", "powerserver")
    proxy_url = os.environ.get("DOCKER_PROXY_URL", "tcp://docker-socket-proxy:2375")

    config = ControllerConfig.load(config_path)

    private_key = base64.b64decode(_require("GITHUB_RUNNER_APP_PRIVATE_KEY_B64")).decode()
    github = GitHubAdapter(
        app_id=_require("GITHUB_RUNNER_APP_ID"),
        installation_id=_require("GITHUB_RUNNER_APP_INSTALLATION_ID"),
        private_key_pem=private_key,
    )
    docker_client = docker.DockerClient(base_url=proxy_url)
    docker_adapter = DockerAdapter(client=docker_client, config=config, host=host)

    controller = Controller(config=config, github=github, docker=docker_adapter, ledger=Ledger())
    app = create_app(controller, poll_interval=poll_interval)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
