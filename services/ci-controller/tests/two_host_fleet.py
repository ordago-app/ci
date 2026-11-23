"""Two generic pool hosts and a controller wired to them, for tests that are about
which host takes work. host-a has the larger budget, so it is where admission sends a
job first."""

from __future__ import annotations

from unittest.mock import MagicMock

from src import controller as controller_module
from src.config import ControllerConfig
from src.controller import Controller
from src.docker_adapter import RunnerImageMissing
from src.github_adapter import RunnerList
from src.ledger import Ledger
from src.models import QueuedJob
from src.scheduler import LocalScheduler

IMAGE = "example/runner:latest"

TWO_HOST_CONFIG = """\
ram_budget_mb: 11000
max_concurrent_lanes: 8
default_class: light
default_host: host-a
runner_image: example/runner:latest
work_dirs:
  ssd: /work/ssd
  hdd: /work/hdd
classes:
  light:
    ram_mb: 700
  heavy:
    ram_mb: 4000
review_admission:
  cheap_classes: [light]
  session_host: host-a
  session_ram_mb: 600
repos:
  - project: ordago-apps
    label_class:
      ordago-ci: light
      heavy: heavy
hosts:
  host-a:
    docker_endpoint: tcp://host-a:2375
  host-b:
    docker_endpoint: tcp://host-b:2375
    ram_budget_mb: 6000
"""


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Fleet:
    """Both hosts behind a DockerPool double, the controller's clock and the
    scheduler's on one `now` the test moves."""

    def __init__(self, write_config, monkeypatch) -> None:
        self.now = 10_000.0
        monkeypatch.setattr(controller_module, "_now", lambda: self.now)
        self.config = ControllerConfig.load(write_config(TWO_HOST_CONFIG))
        self.queued: list[QueuedJob] = []
        github = MagicMock()
        github.list_queued_jobs.side_effect = lambda repo: [
            j for j in self.queued if j.repo == repo
        ]
        github.mint_registration_token.return_value = "ARRT"
        github.list_runners.return_value = RunnerList(runners=[], complete=True)
        self.adapters: dict[str, MagicMock] = {}
        for name in ("host-a", "host-b"):
            adapter = MagicMock()
            adapter.list_lanes.return_value = []
            adapter.sample.return_value = None
            adapter.count_review_sessions.return_value = 0
            adapter.spawn.side_effect = lambda decision, registration_token, _n=name: (
                f"{_n}-cici-{decision.job.job_id}",
                f"cid-{_n}-{decision.job.job_id}",
            )
            self.adapters[name] = adapter
        self.up = {"host-a", "host-b"}
        pool = MagicMock()
        pool.for_host.side_effect = lambda name: self.adapters[name]
        pool.ram_offers.return_value = {}
        pool.snapshot.side_effect = lambda: (
            set(self.up),
            {n: a.list_lanes() for n, a in self.adapters.items() if n in self.up},
        )
        reviews = MagicMock()
        reviews.head_states.return_value = {}
        self.ledger = Ledger()
        self.scheduler = LocalScheduler(self.config, self.ledger, clock=lambda: self.now)
        self.ctrl = Controller(
            host="host-a",
            config=self.config,
            github=github,
            docker=pool,
            scheduler=self.scheduler,
            reviews=reviews,
        )

    def queue(self, *job_ids: int) -> None:
        self.queued = [
            QueuedJob(job_id=i, repo="ordago-app/ordago-apps", labels=["ordago-ci"])
            for i in job_ids
        ]

    def image_missing_on(self, host: str) -> None:
        self.adapters[host].spawn.side_effect = RunnerImageMissing(
            host, IMAGE, Exception("403 Client Error: Forbidden for url: /images/create")
        )

    def host_status(self, host: str) -> dict:
        return self.ctrl.status()["hosts"][host]
