from unittest.mock import MagicMock

from src.config import ControllerConfig
from src.controller import Controller
from src.docker_adapter import LaneInfo
from src.ledger import Ledger
from src.models import AdmitDecision, DeferDecision, QueuedJob, Reservation
from tests.conftest import VALID_CONFIG


def _controller(write_config, queued):
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    github = MagicMock()
    github.list_queued_jobs.side_effect = lambda repo: [j for j in queued if j.repo == repo]
    github.mint_registration_token.return_value = "ARRT"
    docker = MagicMock()
    docker.list_lanes.return_value = []
    docker.spawn.side_effect = lambda decision, registration_token: f"powerserver-cici-{decision.job.job_id}"
    return Controller(config=cfg, github=github, docker=docker, ledger=Ledger()), github, docker


def test_tick_admits_and_spawns(write_config) -> None:
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["self-hosted", "homelab"])
    ctrl, github, docker = _controller(write_config, [job])

    decisions = ctrl.tick()

    assert any(isinstance(d, AdmitDecision) for d in decisions)
    github.mint_registration_token.assert_called_once_with("alvaro-francisco-gil/homelab")
    docker.spawn.assert_called_once()
    assert ctrl.ledger.has_job(1)


def test_tick_does_not_respawn_running_job(write_config) -> None:
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    ctrl, _github, docker = _controller(write_config, [job])
    ctrl.ledger.add(Reservation("powerserver-cici-1", 1, job.repo, "light", 700, False))
    # The lane is genuinely running, so reconcile must see it.
    docker.list_lanes.return_value = [LaneInfo("powerserver-cici-1", 1, "cid")]

    decisions = ctrl.tick()

    assert all(isinstance(d, DeferDecision) for d in decisions)
    docker.spawn.assert_not_called()


def test_reconcile_drops_finished_lane(write_config) -> None:
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    ctrl, _github, docker = _controller(write_config, [])
    ctrl.ledger.add(Reservation("powerserver-cici-1", 1, job.repo, "light", 700, False))
    docker.list_lanes.return_value = []  # lane finished + auto-removed

    ctrl.reconcile()

    assert ctrl.ledger.lane_count() == 0


def test_reconcile_readopts_orphan_lane(write_config) -> None:
    ctrl, _github, docker = _controller(write_config, [])
    # Controller restarted; a lane is running but the ledger is empty.
    docker.list_lanes.return_value = [LaneInfo("powerserver-cici-5", 5, "cid")]

    ctrl.reconcile()

    assert ctrl.ledger.has_job(5)


def test_spawn_failure_does_not_add_to_ledger(write_config) -> None:
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    ctrl, _github, docker = _controller(write_config, [job])
    docker.spawn.side_effect = RuntimeError("docker down")

    ctrl.tick()

    assert ctrl.ledger.lane_count() == 0
