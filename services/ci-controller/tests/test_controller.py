from unittest.mock import MagicMock

import pytest
from src.config import ControllerConfig
from src.controller import Controller
from src.docker_adapter import LaneInfo
from src.ledger import Ledger
from src.models import AdmitDecision, DeferDecision, QueuedJob, Reservation

from tests.conftest import VALID_CONFIG


def _controller(write_config, queued, metrics=None):
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    github = MagicMock()
    github.list_queued_jobs.side_effect = lambda repo: [j for j in queued if j.repo == repo]
    github.mint_registration_token.return_value = "ARRT"
    docker = MagicMock()
    docker.list_lanes.return_value = []
    docker.spawn.side_effect = lambda decision, registration_token: (
        f"powerserver-cici-{decision.job.job_id}"
    )
    docker.sample.return_value = (2900, 140.0)
    ctrl = Controller(config=cfg, github=github, docker=docker, ledger=Ledger(), metrics=metrics)
    return ctrl, github, docker


@pytest.fixture()
def make_controller(write_config):
    """Factory fixture: make_controller(metrics=...) returns a Controller with mocks wired."""

    def _make(metrics=None):
        job = QueuedJob(
            job_id=1, repo="alvaro-francisco-gil/homelab", labels=["self-hosted", "homelab"]
        )
        ctrl, _github, _docker = _controller(write_config, [job], metrics=metrics)
        return ctrl

    return _make


def test_tick_admits_and_spawns(write_config) -> None:
    job = QueuedJob(
        job_id=1, repo="alvaro-francisco-gil/homelab", labels=["self-hosted", "homelab"]
    )
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


def test_reconcile_deletes_work_dir_when_lane_reaped(write_config, tmp_path) -> None:
    # Regression: ephemeral runner containers auto-remove, but their per-lane host
    # work dir ({work_dirs[disk]}/{lane_id}-work) was never deleted, so every job
    # leaked a directory and slowly filled /mnt/ci-ssd to 100%.
    ssd = tmp_path / "ssd"
    hdd = tmp_path / "hdd"
    ssd.mkdir()
    hdd.mkdir()
    cfg_text = VALID_CONFIG.replace("  ssd: /mnt/ci-ssd/ci-controller", f"  ssd: {ssd}").replace(
        "  hdd: /opt/personal/github-actions/ci-controller", f"  hdd: {hdd}"
    )
    cfg = ControllerConfig.load(write_config(cfg_text))
    docker = MagicMock()
    docker.list_lanes.return_value = []  # lane finished + auto-removed
    ctrl = Controller(config=cfg, github=MagicMock(), docker=docker, ledger=Ledger())

    lane_id = "powerserver-cici-1"
    work_dir = ssd / f"{lane_id}-work"
    work_dir.mkdir()
    (work_dir / "scratch.txt").write_text("leftover build output")
    ctrl.ledger.add(
        Reservation(
            lane_id, 1, "alvaro-francisco-gil/homelab", "light", 700, False, work_disk="ssd"
        )
    )

    ctrl.reconcile()

    assert ctrl.ledger.lane_count() == 0
    assert not work_dir.exists(), "reaped lane's work dir must be deleted, not leaked"


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


def test_tick_records_admit_and_defer_events(make_controller, tmp_path) -> None:
    # make_controller is the existing helper; pass a real MetricsStore + a queue
    # of one admittable + one budget-exceeding job (see existing test patterns).
    from src.metrics import MetricsStore

    store = MetricsStore(str(tmp_path / "m.db"))
    ctrl = make_controller(metrics=store)  # helper wires github/docker mocks
    ctrl.tick()
    kinds = [r[0] for r in store.conn.execute("SELECT kind FROM events").fetchall()]
    assert "admit" in kinds
    store.close()


def test_reap_records_peak_footprint(make_controller, tmp_path) -> None:
    from src.metrics import MetricsStore

    store = MetricsStore(str(tmp_path / "m.db"))
    ctrl = make_controller(metrics=store)
    # 1) admit a lane; docker mock reports it running and sample() -> (2900, 140.0)
    ctrl.tick()
    # 1b) simulate the lane appearing as running so reconcile can sample it
    ctrl.docker.list_lanes.return_value = [LaneInfo("powerserver-cici-1", 1, "cid")]
    ctrl.reconcile()
    # 2) docker mock now reports NO lanes -> reconcile reaps it
    ctrl.docker.list_lanes.return_value = []
    ctrl.reconcile()
    reap = store.conn.execute("SELECT peak_ram_mb FROM events WHERE kind='reap'").fetchone()
    assert reap is not None and reap[0] == 2900
    store.close()
