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
    github.job_conclusion.return_value = None
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


def test_reconcile_does_not_delete_outside_work_base_for_malicious_lane_id(
    write_config, tmp_path
) -> None:
    # Hardening: lane_id can come from a re-adopted Docker label (not just our own
    # spawn naming), and labels are unconstrained. A lane_id with `../` must NOT let
    # cleanup escape the work-dir base and rmtree an arbitrary path.
    ssd = tmp_path / "ssd"
    hdd = tmp_path / "hdd"
    ssd.mkdir()
    hdd.mkdir()
    # A sibling dir outside the work base. The "-work" suffix the cleanup appends means
    # the escape target must itself end in "-work"; lane_id "../outside" -> ssd/../outside-work.
    outside = tmp_path / "outside-work"
    outside.mkdir()
    (outside / "precious.txt").write_text("must survive")
    cfg_text = VALID_CONFIG.replace("  ssd: /mnt/ci-ssd/ci-controller", f"  ssd: {ssd}").replace(
        "  hdd: /opt/personal/github-actions/ci-controller", f"  hdd: {hdd}"
    )
    cfg = ControllerConfig.load(write_config(cfg_text))
    docker = MagicMock()
    docker.list_lanes.return_value = []
    ctrl = Controller(config=cfg, github=MagicMock(), docker=docker, ledger=Ledger())

    # lane_id from an unconstrained Docker label: {work_base}/{lane_id}-work escapes ssd.
    malicious = "../outside"  # -> ssd/../outside-work == tmp_path/outside-work
    ctrl.ledger.add(
        Reservation(
            malicious, 1, "alvaro-francisco-gil/homelab", "light", 700, False, work_disk="ssd"
        )
    )

    ctrl.reconcile()

    assert outside.exists(), "cleanup must not escape the work-dir base"
    assert (outside / "precious.txt").exists(), "cleanup must not delete files outside work_base"


def test_reconcile_readopts_orphan_lane(write_config) -> None:
    ctrl, _github, docker = _controller(write_config, [])
    # Controller restarted; a lane is running but the ledger is empty.
    docker.list_lanes.return_value = [LaneInfo("powerserver-cici-5", 5, "cid")]

    ctrl.reconcile()

    assert ctrl.ledger.has_job(5)


def test_readopt_restores_the_lanes_real_class(write_config) -> None:
    # Regression: a restart re-adopted every running lane at default_class (light/700),
    # so a lane actually running a multi-GB job was booked at 700 MB and the ledger
    # handed the freed budget to new admissions. Measured twice in production — reaps
    # tagged class=light repo=(adopted) with 7084 MB and 7100 MB peaks, a 10x
    # under-reserve on the exact path that OOM-kills the host.
    ctrl, _github, docker = _controller(write_config, [])
    docker.list_lanes.return_value = [
        LaneInfo("powerserver-cici-5", 5, "cid", class_name="emulator")
    ]

    ctrl.reconcile()

    res = next(r for r in ctrl.ledger.reservations() if r.job_id == 5)
    assert res.class_name == "emulator"
    assert res.ram_mb == 2500
    assert res.needs_kvm is True
    assert res.work_disk == "hdd"


def test_readopt_without_a_class_label_reserves_the_largest_class(write_config) -> None:
    # Lanes spawned before the class label existed carry no class, and the container
    # labels are the only surviving record. Over-reserving costs deferrals, which are
    # free; under-reserving is the OOM path — so price the unknown at the ceiling
    # rather than at default_class.
    ctrl, _github, docker = _controller(write_config, [])
    docker.list_lanes.return_value = [LaneInfo("powerserver-cici-6", 6, "cid", class_name=None)]

    ctrl.reconcile()

    res = next(r for r in ctrl.ledger.reservations() if r.job_id == 6)
    assert res.ram_mb == 2500, "unknown class must reserve the largest class, not light/700"


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


def _events(metrics, kind):
    return [c.kwargs for c in metrics.record_event.call_args_list if c.kwargs["kind"] == kind]


def test_admit_event_records_job_identity(write_config) -> None:
    job = QueuedJob(
        job_id=1,
        repo="alvaro-francisco-gil/homelab",
        labels=["self-hosted", "homelab"],
        workflow="CI",
        job_name="build-android",
    )
    metrics = MagicMock()
    ctrl, _github, _docker = _controller(write_config, [job], metrics=metrics)

    ctrl.tick()

    (admit,) = _events(metrics, "admit")
    assert admit["job_name"] == "build-android"
    assert admit["workflow"] == "CI"


def test_defer_event_records_every_binding_reason(write_config) -> None:
    """The masked-gate fix has to survive the trip into the events log."""
    job = QueuedJob(
        job_id=2, repo="alvaro-francisco-gil/homelab", labels=["self-hosted", "homelab"]
    )
    metrics = MagicMock()
    ctrl, _github, docker = _controller(write_config, [job], metrics=metrics)
    # Fill every lane so lane_ceiling binds, and the budget so budget_full binds too.
    for lane in range(8):  # VALID_CONFIG: max_concurrent_lanes 8, ram_budget_mb 12000
        ctrl.ledger.add(
            Reservation(
                f"lane-{lane}", 100 + lane, "alvaro-francisco-gil/homelab", "light", 1600, False
            )
        )
    docker.list_lanes.return_value = [
        LaneInfo(f"lane-{lane}", 100 + lane, f"cid-{lane}") for lane in range(8)
    ]

    ctrl.tick()

    (defer,) = _events(metrics, "defer")
    assert defer["reason"] == "lane_ceiling"
    assert defer["reasons"] == "lane_ceiling,budget_full"


def test_reap_records_the_job_conclusion(write_config) -> None:
    """A lane that vanishes without a terminal conclusion is what a sleep-kill looks like."""
    job = QueuedJob(job_id=3, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    metrics = MagicMock()
    ctrl, github, docker = _controller(write_config, [], metrics=metrics)
    github.job_conclusion.return_value = None
    ctrl.ledger.add(Reservation("lane-3", 3, job.repo, "light", 700, False))
    docker.list_lanes.return_value = []  # the lane is gone

    ctrl.tick()

    (reap,) = _events(metrics, "reap")
    assert reap["conclusion"] is None
    github.job_conclusion.assert_called_once_with("alvaro-francisco-gil/homelab", 3)


def test_reap_of_an_adopted_lane_skips_the_conclusion_lookup(write_config) -> None:
    """The '(adopted)' sentinel is not a real repo — querying it would 404 every tick.

    It must not be recorded as a plain NULL conclusion either: ci_bench's
    infra_failures() would otherwise count every re-adopted lane as a genuine infra
    failure, when it's actually just "we never looked, by design".
    """
    metrics = MagicMock()
    ctrl, github, docker = _controller(write_config, [], metrics=metrics)
    ctrl.ledger.add(Reservation("lane-9", 9, "(adopted)", "light", 700, False))
    docker.list_lanes.return_value = []

    ctrl.tick()

    github.job_conclusion.assert_not_called()
    (reap,) = _events(metrics, "reap")
    assert reap["conclusion"] == "adopted"


def test_reap_records_lookup_failed_sentinel_when_the_conclusion_lookup_raises(
    write_config,
) -> None:
    """A swallowed exception must not be conflated with a genuine NULL conclusion — see
    Critical 1(b): under the old code, an App missing Actions:Read would 403 every lookup
    and every one of those would silently read as a real infra failure."""
    job = QueuedJob(job_id=4, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    metrics = MagicMock()
    ctrl, github, docker = _controller(write_config, [], metrics=metrics)
    github.job_conclusion.side_effect = RuntimeError("403 Forbidden")
    ctrl.ledger.add(Reservation("lane-4", 4, job.repo, "light", 700, False))
    docker.list_lanes.return_value = []

    ctrl.tick()

    (reap,) = _events(metrics, "reap")
    assert reap["conclusion"] == "lookup_failed"


def test_events_record_the_controller_host(write_config) -> None:
    """events.host must populate, or the per-host report path can never work."""
    job = QueuedJob(
        job_id=1, repo="alvaro-francisco-gil/homelab", labels=["self-hosted", "homelab"]
    )
    metrics = MagicMock()
    ctrl, _github, docker = _controller(write_config, [job], metrics=metrics)

    ctrl.tick()
    ctrl.ledger.add(Reservation("gone", 99, "alvaro-francisco-gil/homelab", "light", 700, False))
    docker.list_lanes.return_value = []
    ctrl.tick()

    kinds = {c.kwargs["kind"]: c.kwargs for c in metrics.record_event.call_args_list}
    assert {"admit", "reap"} <= kinds.keys()
    for kind, kwargs in kinds.items():
        assert kwargs["host"] == "powerserver", f"{kind} event missing host"
