import itertools
import logging
from collections import defaultdict
from unittest.mock import MagicMock

import pytest
from src.admission import evaluate
from src.config import ControllerConfig
from src.controller import ATTRIBUTION_WARN_AFTER, Controller
from src.docker_adapter import LaneInfo
from src.github_adapter import JobLookup, RunnerInfo, RunnerList
from src.ledger import Ledger
from src.models import (
    INFRA_FAILURE,
    LANE_LOST_UNATTRIBUTED,
    AdmitDecision,
    DeferDecision,
    QueuedJob,
    Reservation,
    RunningJob,
)

from tests.conftest import VALID_CONFIG

MULTI_HOST_CONFIG = (
    VALID_CONFIG
    + """\
hosts:
  powerserver:
    docker_endpoint: tcp://docker-socket-proxy:2375
  desktop:
    docker_endpoint: tcp://desktop:2375
"""
)


def _pool(docker, host="powerserver"):
    """Wrap a single mock DockerAdapter as a single-host DockerPool double.

    `for_host` returns `docker` ONLY for `host`, and a distinct throwaway mock for any
    other name. Returning the same adapter for every name would make a misrouted
    `remove()` land on the right mock by accident, so no routing assertion built on this
    double could ever fail — which is exactly how the reaper's cross-host removal went
    untested. `pool.strangers` records what was handed out instead.
    """
    pool = MagicMock()
    pool.up = {host}  # test knob: which hosts answer this tick
    pool.strangers = {}

    def _for_host(name):
        if name == host:
            return docker
        return pool.strangers.setdefault(name, MagicMock(name=f"adapter-{name}"))

    pool.for_host.side_effect = _for_host
    pool.snapshot.side_effect = lambda: (
        set(pool.up),
        {host: docker.list_lanes()} if host in pool.up else {},
    )
    return pool


def _controller(write_config, queued, metrics=None):
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    github = MagicMock()
    github.list_queued_jobs.side_effect = lambda repo: [j for j in queued if j.repo == repo]
    github.mint_registration_token.return_value = "ARRT"
    github.job_conclusion.return_value = None
    github.list_runners.return_value = RunnerList(runners=[], complete=True)
    github.find_job_for_runner.return_value = JobLookup(job=None, complete=True)
    docker = MagicMock()
    docker.list_lanes.return_value = []
    # Mirrors DockerAdapter.spawn: the job id is a readable prefix, not the identity.
    # Two lanes can exist for the same job (GitHub gave the first one a different job).
    spawns = itertools.count(1)

    def _spawn(decision, registration_token):
        n = next(spawns)
        return f"powerserver-cici-{decision.job.job_id}-{n:06x}", f"cid-{n}"

    docker.spawn.side_effect = _spawn
    docker.sample.return_value = (2900, 140.0)
    ctrl = Controller(
        config=cfg, github=github, docker=_pool(docker), ledger=Ledger(), metrics=metrics
    )
    return ctrl, github, docker


def _multi_host_controller(write_config, queued, metrics=None):
    """Two healthy hosts (powerserver, desktop) wired as a DockerPool double.

    Tests mutate `pool.up` to simulate a host going away, and
    `adapters[name].list_lanes.return_value` to simulate lanes on each host.

    snapshot() returns health and lanes from ONE derived source here, mirroring the
    real DockerPool. A double that let the two disagree (or that could not express a
    disagreement) would hide the reap path this suite exists to pin.
    """
    cfg = ControllerConfig.load(write_config(MULTI_HOST_CONFIG))
    github = MagicMock()
    github.list_queued_jobs.side_effect = lambda repo: [j for j in queued if j.repo == repo]
    github.mint_registration_token.return_value = "ARRT"
    github.job_conclusion.return_value = None

    adapters = {}
    for name in ("powerserver", "desktop"):
        adapter = MagicMock()
        adapter.list_lanes.return_value = []
        adapter.spawn.side_effect = lambda decision, registration_token, _name=name: (
            f"{_name}-cici-{decision.job.job_id}",
            f"cid-{_name}-{decision.job.job_id}",
        )
        adapter.sample.return_value = (2900, 140.0)
        adapters[name] = adapter

    pool = MagicMock()
    pool.for_host.side_effect = lambda name: adapters[name]
    pool.up = {"powerserver", "desktop"}
    pool.snapshot.side_effect = lambda: (
        set(pool.up),
        {name: a.list_lanes() for name, a in adapters.items() if name in pool.up},
    )
    ctrl = Controller(config=cfg, github=github, docker=pool, ledger=Ledger(), metrics=metrics)
    return ctrl, github, adapters, pool


@pytest.fixture()
def make_controller(write_config):
    """Factory fixture: make_controller(metrics=...) returns (ctrl, docker) with mocks wired.

    `docker` is the underlying mock DockerAdapter (not the DockerPool double at
    ctrl.docker), since that's what callers need to poke list_lanes()/sample() on.
    """

    def _make(metrics=None):
        job = QueuedJob(
            job_id=1, repo="alvaro-francisco-gil/homelab", labels=["self-hosted", "homelab"]
        )
        ctrl, _github, docker = _controller(write_config, [job], metrics=metrics)
        return ctrl, docker

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
    ctrl = Controller(config=cfg, github=MagicMock(), docker=_pool(docker), ledger=Ledger())

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
    ctrl = Controller(config=cfg, github=MagicMock(), docker=_pool(docker), ledger=Ledger())

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

    res = next(r for r in ctrl.ledger.reservations() if r.spawned_for_job_id == 5)
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

    res = next(r for r in ctrl.ledger.reservations() if r.spawned_for_job_id == 6)
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
    ctrl, _docker = make_controller(metrics=store)  # helper wires github/docker mocks
    ctrl.tick()
    kinds = [r[0] for r in store.conn.execute("SELECT kind FROM events").fetchall()]
    assert "admit" in kinds
    store.close()


def test_reap_records_peak_footprint(make_controller, tmp_path) -> None:
    from src.metrics import MetricsStore

    store = MetricsStore(str(tmp_path / "m.db"))
    ctrl, docker = make_controller(metrics=store)
    # 1) admit a lane; docker mock reports it running and sample() -> (2900, 140.0)
    ctrl.tick()
    # 1b) simulate the lane appearing as running so reconcile can sample it. The lane id
    # is the adapter's to choose (it carries a per-spawn suffix), so read it off the ledger.
    (admitted,) = ctrl.ledger.reservations()
    docker.list_lanes.return_value = [LaneInfo(admitted.lane_id, 1, "cid", class_name="light")]
    ctrl.reconcile()
    # 2) docker mock now reports NO lanes -> reconcile reaps it
    docker.list_lanes.return_value = []
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
    ctrl.ledger.add(Reservation("lane-3", 3, job.repo, "light", 700, False, running_job_id=3))
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
    ctrl.ledger.add(Reservation("lane-4", 4, job.repo, "light", 700, False, running_job_id=4))
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


def test_readopt_reserves_the_largest_class_when_the_label_names_a_retired_class(
    write_config,
) -> None:
    """A class removed from config between spawn and restart must not crash reconcile.

    Distinct from the no-label case: the lane DOES carry a class, it just no longer
    exists. Same conservative answer — an unknown reserve is priced at the ceiling,
    because over-reserving costs deferrals and under-reserving OOMs the host.
    """
    ctrl, _github, docker = _controller(write_config, [])
    docker.list_lanes.return_value = [LaneInfo("p-cici-7", 7, "cid", class_name="retired-class")]

    ctrl.reconcile()

    (res,) = ctrl.ledger.reservations()
    assert res.ram_mb == 2500, "a retired class name must not be priced at light/700"


def test_a_host_that_stops_responding_has_its_lanes_reaped_as_infra_failures(write_config) -> None:
    """Ledger frees the budget; one reap event per lane with conclusion='infra_failure';
    github.job_conclusion is never called."""
    from src.models import INFRA_FAILURE

    metrics = MagicMock()
    ctrl, github, _adapters, pool = _multi_host_controller(write_config, [], metrics=metrics)
    ctrl.ledger.add(
        Reservation(
            "desktop-cici-1",
            1,
            "alvaro-francisco-gil/homelab",
            "light",
            700,
            False,
            host="desktop",
            # Attributed: only a lane whose job we actually observed can carry a
            # JOB-level verdict. The unattributed case is a lane-level loss and is
            # covered by its own test below.
            running_job_id=1,
        )
    )
    pool.up = {"powerserver"}  # desktop went away mid-flight

    # Declaring a host lost is debounced (host_unhealthy_ticks), so drive enough
    # consecutive failing checks to cross the threshold.
    for _ in range(ctrl.config.host_unhealthy_ticks):
        ctrl.reconcile()

    assert ctrl.ledger.lane_count() == 0
    (reap,) = _events(metrics, "reap")
    assert reap["conclusion"] == INFRA_FAILURE
    assert reap["host"] == "desktop"
    github.job_conclusion.assert_not_called()


def test_health_and_lane_listing_come_from_one_observation(write_config) -> None:
    """A host cannot be reported healthy while its lanes are unlistable.

    Two separate passes (ping, then list) can disagree: ping succeeds, the listing
    then fails, and the host looks healthy with zero lanes — indistinguishable from
    "every lane finished". The reaper would free live reservations after one blip,
    bypassing the host_unhealthy_ticks debounce entirely. snapshot() is the fix, so
    pin the invariant on the real DockerPool rather than on a double that cannot
    express the disagreement.
    """
    import docker.errors
    from src.config import ControllerConfig as _Config
    from src.docker_adapter import DockerPool

    cfg = _Config.load(write_config(MULTI_HOST_CONFIG))
    # Pre-created: clients are built lazily on first use, so a factory that populated
    # this dict on call would leave it empty at configure time.
    clients = defaultdict(MagicMock)

    def _factory(endpoint):
        clients[endpoint].ping.return_value = True
        return clients[endpoint]

    pool = DockerPool(cfg, client_factory=_factory)
    # The desktop answers ping but cannot list: exactly the inconsistent middle state.
    desktop = clients["tcp://desktop:2375"]
    desktop.containers.list.side_effect = docker.errors.DockerException("connection reset")
    clients["tcp://docker-socket-proxy:2375"].containers.list.return_value = []

    healthy, lanes = pool.snapshot()

    assert "desktop" not in healthy, "a host that cannot list its lanes is not healthy"
    assert "desktop" not in lanes
    assert healthy == {"powerserver"}


def test_a_single_failed_health_check_never_reaps_a_running_lane(write_config) -> None:
    """Regression: one blip must not free budget or record a false infra failure.

    powerserver's own socket-proxy lives in the controller's compose stack, so an
    ordinary `make deploy` recreates it and produces exactly one failing tick while
    every lane keeps running. Reaping on the first miss would free live lanes' budget
    — the phantom-budget hazard the class-aware _readopt fix closed — and permanently
    mis-record a job that went green as an infra failure.
    """
    metrics = MagicMock()
    ctrl, github, adapters, pool = _multi_host_controller(write_config, [], metrics=metrics)
    ctrl.ledger.add(
        Reservation(
            "desktop-cici-1", 1, "alvaro-francisco-gil/homelab", "light", 700, False, host="desktop"
        )
    )

    pool.up = {"powerserver"}  # one blip...
    ctrl.reconcile()

    assert ctrl.ledger.lane_count() == 1, "a blip must not free the reservation"
    assert ctrl.ledger.total_ram(host="desktop") == 700
    assert _events(metrics, "reap") == [], "no outcome may be recorded while merely blind"

    # ...and the host answers again. The miss counter resets, so the lane survives
    # indefinitely rather than dying on an accumulation of unrelated blips.
    pool.up = {"powerserver", "desktop"}
    adapters["desktop"].list_lanes.return_value = [LaneInfo("desktop-cici-1", 1, "cid")]
    ctrl.reconcile()

    assert ctrl.ledger.lane_count() == 1
    assert ctrl._unhealthy_ticks["desktop"] == 0
    github.job_conclusion.assert_not_called()


def test_defer_events_record_the_closest_host_so_the_per_host_gate_table_populates(
    write_config,
) -> None:
    """ci_bench builds "Binding Gates by Host" from `kind='defer' AND host IS NOT NULL`.

    Emitting every defer with host=NULL made that whole report section dead surface: it
    could only ever contain rows a test synthesized directly into the DB, never one the
    controller produced. This asserts the real tick() path, not a hand-written row.
    """
    metrics = MagicMock()
    # Both hosts inherit max_concurrent_lanes: 8, so 16 lanes fit fleet-wide; queue more
    # than that and the surplus defers on lane_ceiling — a genuine capacity verdict.
    jobs = [
        QueuedJob(job_id=i, repo="alvaro-francisco-gil/homelab", labels=["self-hosted", "homelab"])
        for i in range(1, 21)
    ]
    ctrl, _github, _adapters, _pool = _multi_host_controller(write_config, jobs, metrics=metrics)

    ctrl.tick()

    capacity_defers = [
        e for e in _events(metrics, "defer") if e["reason"] not in ("already_running",)
    ]
    assert capacity_defers, "expected at least one capacity defer"
    for event in capacity_defers:
        assert event["host"] in ("powerserver", "desktop"), (
            f"capacity defer recorded host={event['host']!r}; the per-host gate table "
            "filters on host IS NOT NULL and would never see it"
        )


def test_non_capacity_defers_belong_to_no_host(write_config) -> None:
    """not_allowlisted is a verdict about the job, not about any host's capacity.

    Attributing it to a host would inflate that host's gate counts with rows that say
    nothing about its capacity.
    """
    metrics = MagicMock()
    job = QueuedJob(job_id=99, repo="alvaro-francisco-gil/homelab", labels=["ubuntu-latest"])
    ctrl, _github, _adapters, _pool = _multi_host_controller(write_config, [job], metrics=metrics)

    ctrl.tick()

    (defer,) = _events(metrics, "defer")
    assert defer["reason"] == "not_allowlisted"
    assert defer["host"] is None


def test_an_unhealthy_host_is_not_offered_to_evaluate(write_config) -> None:
    job = QueuedJob(
        job_id=1, repo="alvaro-francisco-gil/homelab", labels=["self-hosted", "homelab"]
    )
    ctrl, _github, adapters, pool = _multi_host_controller(write_config, [job])
    pool.up = {"desktop"}  # powerserver (the default host) is down

    decisions = ctrl.tick()

    (admit,) = [d for d in decisions if isinstance(d, AdmitDecision)]
    assert admit.host == "desktop"
    adapters["powerserver"].spawn.assert_not_called()


def test_reap_events_record_the_lanes_host_not_the_controllers(write_config) -> None:
    metrics = MagicMock()
    ctrl, _github, adapters, _pool = _multi_host_controller(write_config, [], metrics=metrics)
    ctrl.ledger.add(
        Reservation(
            "desktop-cici-2", 2, "alvaro-francisco-gil/homelab", "light", 700, False, host="desktop"
        )
    )
    adapters["desktop"].list_lanes.return_value = []  # lane finished normally, host still up

    ctrl.reconcile()

    (reap,) = _events(metrics, "reap")
    assert reap["host"] == "desktop"
    assert ctrl._host == "powerserver"  # the controller's own host, for contrast


def test_status_reports_per_host_lanes_and_budget(write_config) -> None:
    ctrl, _github, adapters, _pool = _multi_host_controller(write_config, [])
    ctrl.ledger.add(
        Reservation(
            "powerserver-cici-9",
            9,
            "alvaro-francisco-gil/homelab",
            "light",
            700,
            False,
            host="powerserver",
        )
    )
    ctrl.ledger.add(
        Reservation(
            "desktop-cici-3", 3, "alvaro-francisco-gil/homelab", "light", 700, False, host="desktop"
        )
    )
    adapters["powerserver"].list_lanes.return_value = [
        LaneInfo("powerserver-cici-9", 9, "cid-a", host="powerserver")
    ]
    adapters["desktop"].list_lanes.return_value = [
        LaneInfo("desktop-cici-3", 3, "cid-b", host="desktop")
    ]

    ctrl.reconcile()
    status = ctrl.status()

    hosts = status["hosts"]
    assert set(hosts) == {"powerserver", "desktop"}
    assert hosts["powerserver"]["lanes"] == 1
    assert hosts["powerserver"]["ram_mb"] == 700
    assert hosts["desktop"]["lanes"] == 1
    assert hosts["desktop"]["ram_mb"] == 700
    assert hosts["powerserver"]["healthy"] is True
    assert hosts["desktop"]["healthy"] is True
    assert hosts["powerserver"]["max_lanes"] == ctrl.config.max_concurrent_lanes
    assert hosts["desktop"]["budget_ram_mb"] == ctrl.config.ram_budget_mb
    # Every existing fleet-wide key must survive unchanged.
    assert status["lanes_running"] == 2
    assert status["ledger_ram_mb"] == 1400


def test_readopt_recovers_the_lanes_host_from_its_label(write_config) -> None:
    ctrl, _github, adapters, _pool = _multi_host_controller(write_config, [])
    adapters["desktop"].list_lanes.return_value = [
        LaneInfo("desktop-cici-11", 11, "cid", host="desktop")
    ]

    ctrl.reconcile()

    (res,) = [r for r in ctrl.ledger.reservations() if r.lane_id == "desktop-cici-11"]
    assert res.host == "desktop"


def test_readopt_falls_back_to_the_adapter_host_when_the_label_is_absent(write_config) -> None:
    """Lanes spawned before HOST_LABEL existed carry no host label; the lane's host is
    recovered from which adapter listed it instead."""
    ctrl, _github, adapters, _pool = _multi_host_controller(write_config, [])
    adapters["desktop"].list_lanes.return_value = [
        LaneInfo("desktop-cici-12", 12, "cid", host=None)
    ]

    ctrl.reconcile()

    (res,) = [r for r in ctrl.ledger.reservations() if r.lane_id == "desktop-cici-12"]
    assert res.host == "desktop"


def _busy_lane(write_config, busy=True, found=None):
    if found is None:
        found = RunningJob(901, "CI", "e2e")
    ctrl, github, docker = _controller(write_config, [])
    ctrl.ledger.add(
        Reservation("powerserver-cici-7", 7, "alvaro-francisco-gil/homelab", "light", 700, False)
    )
    docker.list_lanes.return_value = [LaneInfo("powerserver-cici-7", 7, "cid")]
    github.list_runners.side_effect = lambda repo: RunnerList(
        runners=(
            [RunnerInfo(runner_id=11, name="powerserver-cici-7", busy=busy)]
            if repo == "alvaro-francisco-gil/homelab"
            else []
        ),
        complete=True,
    )
    github.find_job_for_runner.return_value = JobLookup(job=found, complete=True)
    return ctrl, github


def test_reconcile_attributes_the_job_a_busy_lane_actually_took(write_config) -> None:
    ctrl, _github = _busy_lane(write_config)

    ctrl.reconcile()

    (res,) = ctrl.ledger.reservations()
    assert res.running_job_id == 901
    assert res.spawned_for_job_id == 7
    assert (res.workflow, res.job_name) == ("CI", "e2e")
    assert res.idle_since is None
    assert res.runner_id == 11


def test_reconcile_does_not_pay_for_attribution_while_a_lane_is_idle(write_config) -> None:
    ctrl, github = _busy_lane(write_config, busy=False)

    ctrl.reconcile()

    github.find_job_for_runner.assert_not_called()
    (res,) = ctrl.ledger.reservations()
    assert res.running_job_id is None


def test_attribution_is_paid_once_per_lane(write_config) -> None:
    ctrl, github = _busy_lane(write_config)

    ctrl.reconcile()
    ctrl.reconcile()

    assert github.find_job_for_runner.call_count == 1


def test_a_stolen_lane_frees_its_spawned_for_job_for_readmission(write_config) -> None:
    ctrl, _github = _busy_lane(write_config)

    ctrl.reconcile()

    assert not ctrl.ledger.has_job(7)
    assert ctrl.ledger.has_job(901)


def test_reap_reports_the_job_the_lane_ran(write_config) -> None:
    metrics = MagicMock()
    ctrl, github, docker = _controller(write_config, [], metrics=metrics)
    ctrl.ledger.add(
        Reservation(
            "powerserver-cici-7",
            7,
            "alvaro-francisco-gil/homelab",
            "light",
            700,
            False,
            running_job_id=901,
        )
    )
    docker.list_lanes.return_value = []  # lane finished

    ctrl.reconcile()

    kwargs = next(
        c.kwargs for c in metrics.record_event.call_args_list if c.kwargs["kind"] == "reap"
    )
    assert kwargs["job_id"] == 901
    assert kwargs["spawned_for_job_id"] == 7
    assert kwargs["attributed"] == 1
    github.job_conclusion.assert_called_once_with("alvaro-francisco-gil/homelab", 901)


def test_reap_marks_an_unattributed_lane(write_config) -> None:
    metrics = MagicMock()
    ctrl, _github, docker = _controller(write_config, [], metrics=metrics)
    ctrl.ledger.add(
        Reservation("powerserver-cici-7", 7, "alvaro-francisco-gil/homelab", "light", 700, False)
    )
    docker.list_lanes.return_value = []

    ctrl.reconcile()

    kwargs = next(
        c.kwargs for c in metrics.record_event.call_args_list if c.kwargs["kind"] == "reap"
    )
    assert kwargs["attributed"] is None


def test_a_failing_runner_poll_does_not_break_reconcile(write_config) -> None:
    ctrl, github = _busy_lane(write_config)
    github.list_runners.side_effect = RuntimeError("boom")

    ctrl.reconcile()  # must not raise

    assert ctrl.ledger.lane_count() == 1


NOW = 10_000.0
HOMELAB = "alvaro-francisco-gil/homelab"
ORDAGO = "ordago-app/ordago-apps"


def _lane(ctrl, lanes, lane_id, job_id, repo, class_name, ram_mb, **over):
    """Add a reservation and the LaneInfo that keeps reconcile() from dropping it."""
    ctrl.ledger.add(Reservation(lane_id, job_id, repo, class_name, ram_mb, False, **over))
    lanes.append(
        LaneInfo(lane_id, job_id, f"cid-{job_id}", class_name=class_name, host="powerserver")
    )


def _reaper_controller(write_config, queued, monkeypatch, *, idle_for):
    """One idle registered lane (cici-7), plus whatever the test adds via _lane."""
    monkeypatch.setattr("src.controller._now", lambda: NOW)
    ctrl, github, docker = _controller(write_config, queued)
    lanes: list[LaneInfo] = []
    _lane(
        ctrl,
        lanes,
        "powerserver-cici-7",
        7,
        HOMELAB,
        "light",
        700,
        idle_since=NOW - idle_for,
        runner_id=11,
        container_id="cid-7",
    )
    docker.list_lanes.return_value = lanes
    github.list_runners.side_effect = lambda repo: RunnerList(
        runners=(
            [RunnerInfo(runner_id=11, name="powerserver-cici-7", busy=False)]
            if repo == HOMELAB
            else []
        ),
        complete=True,
    )
    github.delete_runner.return_value = True
    return ctrl, github, docker, lanes


def _add_kvm_pressure(ctrl, lanes):
    """Occupy the ledger so a queued emulator job binds BOTH kvm_busy and budget_full.

    VALID_CONFIG: 8 lanes, 12000 MB, emulator = 2500 MB + KVM. Four emulator lanes plus the
    700 MB idle lane leave 1300 MB, so a fifth emulator job binds `budget_full`; it also binds
    `kvm_busy` (all four occupants need KVM). Reaping a light lane cannot free the KVM device,
    so this defer is NOT pressure under the unrelievable-gate rule — it is the honest-but-
    unhelpful case the rule exists for. Lane count stays at 5 of 8, so lane_ceiling never
    enters into it either.
    """
    for i in range(4):
        ctrl.ledger.add(
            Reservation(
                f"powerserver-cici-{i}", i, ORDAGO, "emulator", 2500, True, running_job_id=i
            )
        )
        lanes.append(
            LaneInfo(
                f"powerserver-cici-{i}", i, f"cid-{i}", class_name="emulator", host="powerserver"
            )
        )


def _add_lane_ceiling_pressure(ctrl, lanes):
    """Fill the ledger to max_concurrent_lanes (8 in VALID_CONFIG) with non-KVM, low-RAM
    lanes so a queued job defers on `lane_ceiling` alone. Reaping a lane genuinely relieves
    this gate (it frees a slot), unlike the KVM case above — this is the honest positive-
    pressure fixture."""
    needed = ctrl.config.max_concurrent_lanes - ctrl.ledger.lane_count()
    for i in range(20, 20 + needed):
        ctrl.ledger.add(
            Reservation(f"powerserver-cici-{i}", i, HOMELAB, "light", 700, False, running_job_id=i)
        )
        lanes.append(
            LaneInfo(f"powerserver-cici-{i}", i, f"cid-{i}", class_name="light", host="powerserver")
        )


def _pressure_job():
    return [QueuedJob(job_id=100, repo=ORDAGO, labels=["android-e2e"])]


def test_an_idle_lane_survives_a_quiet_tick(write_config, monkeypatch) -> None:
    """Warm capacity is worth keeping when nothing is waiting for the budget."""
    ctrl, github, docker, _lanes = _reaper_controller(write_config, [], monkeypatch, idle_for=120)

    ctrl.tick()

    github.delete_runner.assert_not_called()
    docker.remove.assert_not_called()
    assert ctrl.ledger.lane_count() == 1


def test_an_idle_lane_is_reaped_when_someone_is_deferred_on_pressure(
    write_config, monkeypatch
) -> None:
    ctrl, github, docker, lanes = _reaper_controller(
        write_config, _pressure_job(), monkeypatch, idle_for=120
    )
    _add_lane_ceiling_pressure(ctrl, lanes)
    # Verify the premise by hand rather than assuming it: the queued job must defer on
    # lane_ceiling alone (no kvm_busy — nothing in the ledger needs KVM).
    decisions = evaluate(_pressure_job(), ctrl.ledger, ctrl.config, None)
    assert decisions[0].reasons == ("lane_ceiling",)

    ctrl.tick()

    github.delete_runner.assert_called_once_with(HOMELAB, 11)
    docker.remove.assert_called_once_with("cid-7")
    assert not any(r.lane_id == "powerserver-cici-7" for r in ctrl.ledger.reservations())


def test_an_idle_lane_survives_pressure_that_reaping_cannot_relieve(
    write_config, monkeypatch
) -> None:
    """A defer binding kvm_busy + budget_full must not trigger a reap: freeing a light
    lane's RAM does not free the KVM device, so the job would just defer again next tick
    while we destroyed a warm lane for nothing."""
    ctrl, github, docker, lanes = _reaper_controller(
        write_config, _pressure_job(), monkeypatch, idle_for=120
    )
    _add_kvm_pressure(ctrl, lanes)
    decisions = evaluate(_pressure_job(), ctrl.ledger, ctrl.config, None)
    assert set(decisions[0].reasons) == {"kvm_busy", "budget_full"}

    ctrl.tick()

    github.delete_runner.assert_not_called()
    docker.remove.assert_not_called()
    assert ctrl.ledger.lane_count() == 5


def test_a_non_pressure_defer_does_not_reap(write_config, monkeypatch) -> None:
    """not_allowlisted recurs on every tick forever (a GitHub-hosted job will never fit
    any class), so counting any defer as pressure — not just a capacity-gate defer —
    would reap warm lanes indefinitely."""
    queued = [QueuedJob(job_id=200, repo=HOMELAB, labels=["ubuntu-latest"])]
    ctrl, github, docker, _lanes = _reaper_controller(
        write_config, queued, monkeypatch, idle_for=120
    )
    decisions = evaluate(queued, ctrl.ledger, ctrl.config, None)
    assert decisions[0].reasons == ("not_allowlisted",)

    ctrl.tick()

    github.delete_runner.assert_not_called()
    docker.remove.assert_not_called()
    assert ctrl.ledger.lane_count() == 1


def test_a_lane_inside_the_grace_window_is_never_reaped_on_pressure(
    write_config, monkeypatch
) -> None:
    """A booting lane is indistinguishable from an idle one from outside."""
    ctrl, github, _docker, lanes = _reaper_controller(
        write_config, _pressure_job(), monkeypatch, idle_for=20
    )
    _add_lane_ceiling_pressure(ctrl, lanes)

    ctrl.tick()

    github.delete_runner.assert_not_called()


def test_a_lane_past_the_absolute_cap_is_reaped_without_pressure(write_config, monkeypatch) -> None:
    ctrl, github, docker, _lanes = _reaper_controller(write_config, [], monkeypatch, idle_for=900)

    ctrl.tick()

    github.delete_runner.assert_called_once_with(HOMELAB, 11)
    docker.remove.assert_called_once_with("cid-7")


def test_a_busy_refusal_leaves_the_lane_alone(write_config, monkeypatch) -> None:
    """422: GitHub says it took a job between our poll and the delete. Never force it."""
    ctrl, github, docker, _lanes = _reaper_controller(write_config, [], monkeypatch, idle_for=900)
    github.delete_runner.return_value = False

    ctrl.tick()

    docker.remove.assert_not_called()
    assert ctrl.ledger.lane_count() == 1


def test_reaping_frees_no_more_lanes_than_the_deferred_demand(write_config, monkeypatch) -> None:
    """Two reapable idle lanes, one deferred job: only the longest-idle one goes.

    Both are inside the absolute cap (600 s), so the only thing that can reap them is
    pressure — which is capped at the number of pressure defers.
    """
    ctrl, github, _docker, lanes = _reaper_controller(
        write_config, _pressure_job(), monkeypatch, idle_for=120
    )
    _lane(
        ctrl,
        lanes,
        "powerserver-cici-8",
        8,
        HOMELAB,
        "light",
        700,
        idle_since=NOW - 300,
        runner_id=12,
        container_id="cid-8",
    )
    _add_lane_ceiling_pressure(ctrl, lanes)

    ctrl.tick()

    assert github.delete_runner.call_count == 1
    assert github.delete_runner.call_args.args[1] == 12  # cici-8, idle 300 s vs cici-7's 120 s


def test_unregistered_lane_inside_the_cap_is_never_reaped(write_config, monkeypatch) -> None:
    """A lane with no runner_id (never registered, or not yet synced) is only ever a
    candidate once past the absolute cap — never via pressure alone, cap or no cap."""
    ctrl, _github, docker, lanes = _reaper_controller(
        write_config, _pressure_job(), monkeypatch, idle_for=30
    )
    _lane(
        ctrl,
        lanes,
        "powerserver-cici-9",
        9,
        HOMELAB,
        "light",
        700,
        idle_since=NOW - 120,
        container_id="cid-9",
    )
    _add_lane_ceiling_pressure(ctrl, lanes)

    ctrl.tick()

    docker.remove.assert_not_called()
    assert any(r.lane_id == "powerserver-cici-9" for r in ctrl.ledger.reservations())


def test_unregistered_lane_confirmed_absent_is_reaped_past_the_cap(
    write_config, monkeypatch, caplog
) -> None:
    """Past the cap, an unregistered lane may be reaped once GitHub's runner list was
    actually consulted this tick and came back without it — a positive absence signal."""
    ctrl, _github, docker, lanes = _reaper_controller(write_config, [], monkeypatch, idle_for=30)
    _lane(
        ctrl,
        lanes,
        "powerserver-cici-9",
        9,
        HOMELAB,
        "light",
        700,
        idle_since=NOW - 900,
        container_id="cid-9",
    )

    with caplog.at_level("WARNING"):
        ctrl.tick()

    docker.remove.assert_called_once_with("cid-9")
    assert not any(r.lane_id == "powerserver-cici-9" for r in ctrl.ledger.reservations())
    assert any("confirmed absent" in rec.message for rec in caplog.records)


def test_unregistered_lane_without_confirmed_absence_survives_past_the_cap(
    write_config, monkeypatch
) -> None:
    """Past the cap, an unregistered lane must NOT be reaped when the runner-list sync
    failed (or never covered its repo) — "we never heard about it" is not "it's gone"."""
    ctrl, github, docker, lanes = _reaper_controller(write_config, [], monkeypatch, idle_for=30)
    _lane(
        ctrl,
        lanes,
        "powerserver-cici-9",
        9,
        HOMELAB,
        "light",
        700,
        idle_since=NOW - 900,
        container_id="cid-9",
    )
    github.list_runners.side_effect = RuntimeError("boom")

    ctrl.tick()

    docker.remove.assert_not_called()
    assert any(r.lane_id == "powerserver-cici-9" for r in ctrl.ledger.reservations())


def test_idle_reap_is_recorded(write_config, monkeypatch) -> None:
    metrics = MagicMock()
    ctrl, _github, _docker, _lanes = _reaper_controller(write_config, [], monkeypatch, idle_for=900)
    ctrl.metrics = metrics

    ctrl.tick()

    reaps = [
        c.kwargs for c in metrics.record_event.call_args_list if c.kwargs["kind"] == "idle_reap"
    ]
    assert len(reaps) == 1
    assert reaps[0]["attributed"] is None  # an idle lane never ran anything to attribute


def test_status_distinguishes_booting_from_busy_lanes(write_config, monkeypatch) -> None:
    monkeypatch.setattr("src.controller._now", lambda: 10_000.0)
    ctrl, _github, _docker = _controller(write_config, [])
    ctrl.ledger.add(
        Reservation("powerserver-cici-7", 7, "o/r", "light", 700, False, idle_since=9_940.0)
    )
    ctrl.ledger.add(
        Reservation("powerserver-cici-8", 8, "o/r", "light", 700, False, running_job_id=901)
    )

    running = {r["lane_id"]: r for r in ctrl.status()["running"]}

    assert running["powerserver-cici-7"]["state"] == "booting"
    assert running["powerserver-cici-7"]["idle_seconds"] == 60
    assert running["powerserver-cici-7"]["job_id"] is None
    assert running["powerserver-cici-8"]["state"] == "busy"
    assert running["powerserver-cici-8"]["job_id"] == 901
    assert running["powerserver-cici-8"]["spawned_for_job_id"] == 8


def test_a_stolen_lane_lets_its_spawned_for_job_take_a_second_distinct_lane(
    write_config, caplog
) -> None:
    """The whole-tick version of the starvation fix. Lane A was spawned for job 7 but
    GitHub gave it job 901; job 7 is still queued, so this tick must put it on a SECOND
    lane — without colliding on lane identity and without logging a spawn error."""
    job = QueuedJob(job_id=7, repo=HOMELAB, labels=["homelab"])
    ctrl, github, docker = _controller(write_config, [job])
    stolen = "powerserver-cici-7-aaaaaa"
    ctrl.ledger.add(Reservation(stolen, 7, HOMELAB, "light", 700, False, container_id="cid-a"))
    docker.list_lanes.return_value = [
        LaneInfo(stolen, 7, "cid-a", class_name="light", host="powerserver")
    ]
    github.list_runners.side_effect = lambda repo: RunnerList(
        runners=[RunnerInfo(runner_id=11, name=stolen, busy=True)] if repo == HOMELAB else [],
        complete=True,
    )
    github.find_job_for_runner.return_value = JobLookup(
        job=RunningJob(901, "CI", "e2e"), complete=True
    )

    with caplog.at_level(logging.WARNING):
        decisions = ctrl.tick()

    assert [d.job.job_id for d in decisions if isinstance(d, AdmitDecision)] == [7]
    lane_ids = {r.lane_id for r in ctrl.ledger.reservations()}
    assert len(lane_ids) == 2, "job 7 must get its own lane, not collide with the stolen one"
    assert stolen in lane_ids
    assert caplog.text == ""
    assert docker.spawn.call_count == 1


def test_a_truncated_runner_list_does_not_confirm_absence(
    write_config, monkeypatch, caplog
) -> None:
    """The reaper force-removes an unregistered lane on the strength of GitHub not
    listing it. A listing that may be missing pages is not evidence of absence — the
    lane it "confirms gone" could be registered and executing a job on page two."""
    ctrl, github, docker, lanes = _reaper_controller(write_config, [], monkeypatch, idle_for=30)
    _lane(
        ctrl,
        lanes,
        "powerserver-cici-9",
        9,
        HOMELAB,
        "light",
        700,
        idle_since=NOW - 900,
        container_id="cid-9",
    )
    github.list_runners.side_effect = lambda repo: RunnerList(
        runners=(
            [RunnerInfo(runner_id=11, name="powerserver-cici-7", busy=False)]
            if repo == HOMELAB
            else []
        ),
        complete=False,
    )

    with caplog.at_level("WARNING"):
        ctrl.tick()

    docker.remove.assert_not_called()
    assert any(r.lane_id == "powerserver-cici-9" for r in ctrl.ledger.reservations())
    assert any("did not confirm it absent" in rec.message for rec in caplog.records)


def test_a_failed_spawn_records_no_admit_event(write_config) -> None:
    """ci_bench.queue_waits() measures first-defer -> admit. An admit row for a lane that
    never existed fabricates an ever-later admission and corrupts the p90/p95."""
    metrics = MagicMock()
    job = QueuedJob(job_id=1, repo=HOMELAB, labels=["homelab"])
    ctrl, _github, docker = _controller(write_config, [job], metrics=metrics)
    docker.spawn.side_effect = RuntimeError("docker down")

    ctrl.tick()

    assert _events(metrics, "admit") == []


def test_a_ledger_insert_failure_records_no_admit_event(write_config, caplog) -> None:
    """The ledger insert must fail the admission the same way a spawn failure does, so a
    spawn that half-succeeds cannot leave the ledger and the event log out of step."""
    metrics = MagicMock()
    job = QueuedJob(job_id=1, repo=HOMELAB, labels=["homelab"])
    ctrl, _github, _docker = _controller(write_config, [job], metrics=metrics)
    ctrl.ledger.add = MagicMock(side_effect=ValueError("lane already in ledger"))

    with caplog.at_level(logging.ERROR):
        ctrl.tick()  # must not raise

    assert _events(metrics, "admit") == []
    assert any("failed to spawn lane" in rec.message for rec in caplog.records)


def test_an_unattributed_reap_does_not_look_up_another_jobs_conclusion(write_config) -> None:
    """Without attribution, claimed_job_id is a prediction. Looking it up returns whatever
    became of a job this lane never ran — often `success`, from the lane that did run it —
    and that answer is then written to this row forever."""
    metrics = MagicMock()
    ctrl, github, docker = _controller(write_config, [], metrics=metrics)
    ctrl.ledger.add(Reservation("powerserver-cici-7-aaaaaa", 7, HOMELAB, "light", 700, False))
    docker.list_lanes.return_value = []

    ctrl.reconcile()

    github.job_conclusion.assert_not_called()
    (reap,) = _events(metrics, "reap")
    assert reap["conclusion"] == "unattributed"
    assert reap["attributed"] is None


def test_a_lane_whose_attribution_never_resolves_is_warned_about(write_config, caplog) -> None:
    """find_job_for_runner scans a bounded page of in_progress runs. Under a backlog —
    exactly when attribution matters — a busy lane's run falls off it and `None` comes
    back every tick. Retrying in silence forever reinstates the defect this branch fixes,
    so the loop has to become visible."""
    ctrl, github = _busy_lane(write_config)
    github.find_job_for_runner.return_value = JobLookup(job=None, complete=True)

    with caplog.at_level(logging.WARNING):
        for _ in range(ATTRIBUTION_WARN_AFTER - 1):
            ctrl.reconcile()
        assert caplog.records == []

        ctrl.reconcile()

    assert github.find_job_for_runner.call_count == ATTRIBUTION_WARN_AFTER
    assert any("unattributed" in rec.message for rec in caplog.records)


def test_a_resolved_attribution_clears_the_unresolved_count(write_config) -> None:
    ctrl, github = _busy_lane(write_config)
    github.find_job_for_runner.return_value = JobLookup(job=None, complete=True)
    ctrl.reconcile()
    github.find_job_for_runner.return_value = JobLookup(
        job=RunningJob(901, "CI", "e2e"), complete=True
    )

    ctrl.reconcile()

    assert ctrl._unresolved_attributions == {}


def test_a_lane_the_ledger_lost_is_readopted_onto_a_routable_host(
    write_config, monkeypatch
) -> None:
    """Regression: the reaper fixtures built LaneInfo positionally, and the field in the
    5th slot became `host` when the second-host rollout landed — so they were passing a
    repo slug as the lane's host. Every affected lane happened to be pre-seeded into the
    ledger, so reconcile() skipped _readopt and nothing noticed. A lane that is NOT
    pre-seeded takes the _readopt path, and the ledger then holds host='<repo slug>',
    which DockerPool.for_host() cannot resolve.
    """
    ctrl, _github, _docker, _lanes = _reaper_controller(
        write_config, [], monkeypatch, idle_for=10.0
    )
    ctrl.ledger.remove("powerserver-cici-7")  # controller restarted; the container remains

    ctrl.reconcile()

    (res,) = ctrl.ledger.reservations()
    assert res.host in ctrl.config.resolved_hosts(), (
        f"re-adopted onto host={res.host!r}, which for_host() cannot resolve"
    )
    ctrl.docker.for_host(res.host)  # must not raise


def _multi_host_reaper(write_config, monkeypatch, *, idle_for, lane_host):
    """One idle registered lane on `lane_host`, in a two-host pool, and nothing queued.

    Callers pass an `idle_for` past `idle_lane_max_seconds`, so the reap needs no
    deferred demand to trigger — which keeps the queue empty and the ledger holding
    exactly the one lane under test.

    Deliberately built on _multi_host_controller rather than _pool(): the reaper's
    host-routing and host-health behaviour cannot be observed at all through a
    single-host double.
    """
    monkeypatch.setattr("src.controller._now", lambda: NOW)
    ctrl, github, adapters, pool = _multi_host_controller(write_config, [])
    ctrl.ledger.add(
        Reservation(
            f"{lane_host}-cici-7",
            7,
            HOMELAB,
            "light",
            700,
            False,
            host=lane_host,
            idle_since=NOW - idle_for,
            runner_id=11,
            container_id="cid-7",
        )
    )
    adapters[lane_host].list_lanes.return_value = [
        LaneInfo(f"{lane_host}-cici-7", 7, "cid-7", class_name="light", host=lane_host)
    ]
    github.list_runners.side_effect = lambda repo: RunnerList(
        runners=(
            [RunnerInfo(runner_id=11, name=f"{lane_host}-cici-7", busy=False)]
            if repo == HOMELAB
            else []
        ),
        complete=True,
    )
    github.delete_runner.return_value = True
    return ctrl, github, adapters, pool


def test_reaping_a_remote_lane_removes_the_container_on_its_own_host(
    write_config, monkeypatch
) -> None:
    """The reaper must route removal through for_host(res.host).

    Before the merge it called a bare `self.docker.remove`, which on a DockerPool is not
    even a method. Routed to the wrong host it would be worse than an error: a container
    id is only meaningful on the daemon that issued it, so a same-id container belonging
    to an unrelated lane could be destroyed.
    """
    ctrl, _github, adapters, _pool = _multi_host_reaper(
        write_config, monkeypatch, idle_for=1200, lane_host="desktop"
    )

    ctrl.tick()

    adapters["desktop"].remove.assert_called_once_with("cid-7")
    adapters["powerserver"].remove.assert_not_called()
    assert ctrl.ledger.lane_count() == 0


def test_an_idle_lane_on_an_unreachable_host_is_never_reaped(write_config, monkeypatch) -> None:
    """ "Idle" is not observable on a host that did not answer this tick.

    reconcile() already holds such a lane's reservation rather than reading it as
    vanished; the reaper taking the opposite view would free the budget of a lane that is
    very likely still running, and would try to reach a daemon that is not there.
    """
    ctrl, github, adapters, pool = _multi_host_reaper(
        write_config, monkeypatch, idle_for=1200, lane_host="desktop"
    )
    pool.up = {"powerserver"}  # the desktop went to sleep

    ctrl.tick()

    assert ctrl.ledger.lane_count() == 1, "a lane we cannot see is not a lane we know is idle"
    assert ctrl.ledger.total_ram(host="desktop") == 700
    adapters["desktop"].remove.assert_not_called()
    github.delete_runner.assert_not_called()


def test_a_remote_lanes_work_dir_is_left_to_its_own_host(write_config, monkeypatch) -> None:
    """The controller can only see its OWN filesystem, so cleaning a path derived from a
    remote lane's id would at best be a no-op and at worst delete a same-named local
    directory. reconcile() already guards its own cleanup this way."""
    ctrl, _github, _adapters, _pool = _multi_host_reaper(
        write_config, monkeypatch, idle_for=1200, lane_host="desktop"
    )
    reaped: list[str] = []
    ctrl._reap_work_dir = lambda lane_id, work_disk: reaped.append(lane_id)  # type: ignore[method-assign]

    ctrl.tick()

    assert reaped == [], "a remote host's work dir belongs to that host's prune unit"


def test_a_local_lanes_work_dir_is_still_cleaned(write_config, monkeypatch) -> None:
    """The other half of the guard: skipping remote hosts must not skip our own."""
    ctrl, _github, _adapters, _pool = _multi_host_reaper(
        write_config, monkeypatch, idle_for=1200, lane_host="powerserver"
    )
    reaped: list[str] = []
    ctrl._reap_work_dir = lambda lane_id, work_disk: reaped.append(lane_id)  # type: ignore[method-assign]

    ctrl.tick()

    assert reaped == ["powerserver-cici-7"]


def test_a_lost_host_reap_is_only_a_job_infra_failure_when_the_lane_was_attributed(
    write_config,
) -> None:
    """A booting lane on a host that sleeps has no observed job — only a prediction.

    Marking it `infra_failure` keys a JOB-level failure to spawned_for_job_id, which is
    exactly the prediction-as-observation defect this branch exists to remove. The lane
    really was lost, so the event is worth recording — but as a LANE-level fact, under
    its own sentinel, and it must not reach infra_failures().
    """
    metrics = MagicMock()
    ctrl, github, _adapters, pool = _multi_host_controller(write_config, [], metrics=metrics)
    ctrl.ledger.add(
        Reservation(
            "desktop-cici-1", 1, HOMELAB, "light", 700, False, host="desktop"
        )  # never attributed: still booting when the desktop slept
    )
    ctrl.ledger.add(
        Reservation(
            "desktop-cici-2", 2, HOMELAB, "light", 700, False, host="desktop", running_job_id=902
        )
    )
    pool.up = {"powerserver"}

    for _ in range(ctrl.config.host_unhealthy_ticks):
        ctrl.reconcile()

    by_lane = {e["lane_id"]: e for e in _events(metrics, "reap")}
    assert by_lane["desktop-cici-2"]["conclusion"] == INFRA_FAILURE
    assert by_lane["desktop-cici-2"]["job_id"] == 902
    assert by_lane["desktop-cici-1"]["conclusion"] == LANE_LOST_UNATTRIBUTED, (
        "an unattributed lost lane must not be recorded as a job-level infra failure"
    )
    github.job_conclusion.assert_not_called()


def test_an_incomplete_attribution_scan_is_reported_on_the_first_poll(write_config, caplog) -> None:
    """ "We stopped looking" is not the benign "the run has not appeared yet" case the
    unresolved counter waits out, so it must not wait 20 polls to be mentioned. Staying
    quiet about it is what let the missing jobs-endpoint pagination go unnoticed."""
    ctrl, github = _busy_lane(write_config)
    github.find_job_for_runner.return_value = JobLookup(job=None, complete=False)

    with caplog.at_level(logging.WARNING):
        ctrl.reconcile()

    assert "INCOMPLETE scan" in caplog.text
    (res,) = ctrl.ledger.reservations()
    assert res.running_job_id is None, "an incomplete scan must not invent an attribution"


def _fill_lanes(ctrl, docker):
    """Occupy every lane slot with reservations that reconcile() will believe.

    A reservation alone is not enough: tick() reconciles before it admits, and a lane
    with no container behind it is treated as vanished and released — so a ledger
    seeded directly would be empty by the time admission ran.
    """
    lanes = []
    for slot in range(ctrl.config.max_concurrent_lanes):
        lane_id = f"powerserver-cici-{slot}"
        ctrl.ledger.add(Reservation(lane_id, slot, HOMELAB, "light", 700, False))
        lanes.append(LaneInfo(lane_id, slot, f"cid-{slot}", class_name="light", host="powerserver"))
    docker.list_lanes.return_value = lanes


def test_status_reports_the_class_mix_behind_each_host(write_config) -> None:
    """The lane list has to say WHICH host and WHICH kind, or a reader cannot split a
    host's totals by class without string-splitting the lane id."""
    ctrl, _github, adapters, _pool = _multi_host_controller(write_config, [])
    ctrl.ledger.add(
        Reservation(
            "powerserver-cici-9",
            9,
            HOMELAB,
            "light",
            700,
            False,
            workflow="CI",
            job_name="ruff",
            host="powerserver",
        )
    )
    ctrl.ledger.add(Reservation("desktop-cici-3", 3, HOMELAB, "light", 700, False, host="desktop"))
    adapters["powerserver"].list_lanes.return_value = [
        LaneInfo("powerserver-cici-9", 9, "cid-a", host="powerserver")
    ]
    adapters["desktop"].list_lanes.return_value = [
        LaneInfo("desktop-cici-3", 3, "cid-b", host="desktop")
    ]
    ctrl.reconcile()

    running = {r["lane_id"]: r for r in ctrl.status()["running"]}

    assert running["powerserver-cici-9"]["host"] == "powerserver"
    assert running["powerserver-cici-9"]["class"] == "light"
    assert running["powerserver-cici-9"]["job_name"] == "ruff"
    assert running["powerserver-cici-9"]["workflow"] == "CI"
    assert running["desktop-cici-3"]["host"] == "desktop"


def test_status_says_what_kind_of_job_is_queued_and_every_gate_holding_it(
    write_config, monkeypatch
) -> None:
    """A queue of opaque ids cannot answer 'is the idle host even allowed to take these'."""
    monkeypatch.setattr("src.controller._now", lambda: 10_000.0)
    # 700, not 7: _fill_lanes occupies job ids 0..max_lanes, and a collision would
    # defer this job as already_running — a different verdict, with no class to report.
    job = QueuedJob(job_id=700, repo=HOMELAB, labels=["homelab"], workflow="CI", job_name="pytest")
    ctrl, _github, _docker = _controller(write_config, [job])
    # Fill the lane ceiling so the job defers on capacity rather than on allowlisting.
    # The lanes must exist in docker too: tick() reconciles first, and a reservation
    # with no container behind it is reaped before admission ever runs.
    _fill_lanes(ctrl, _docker)

    ctrl.tick()
    (queued,) = ctrl.status()["deferred"]

    assert queued["job_id"] == 700
    assert queued["class"] == "light"
    assert queued["job_name"] == "pytest"
    assert queued["workflow"] == "CI"
    assert queued["reason"] in queued["reasons"]  # primary stays the first of the list
    assert queued["host"] == "powerserver"


def test_a_defer_event_records_the_class_so_the_queue_can_be_grouped_later(
    write_config, monkeypatch
) -> None:
    """The column was NULL for every defer row before this, so no capacity report could
    ever answer 'what was the queue made of'."""
    monkeypatch.setattr("src.controller._now", lambda: 10_000.0)
    metrics = MagicMock()
    job = QueuedJob(job_id=700, repo=HOMELAB, labels=["homelab"])  # see the id note above
    ctrl, _github, _docker = _controller(write_config, [job], metrics=metrics)
    _fill_lanes(ctrl, _docker)

    ctrl.tick()

    (defer,) = _events(metrics, "defer")
    assert defer["class_name"] == "light"


def test_status_reports_how_long_each_lane_has_been_running(write_config, monkeypatch) -> None:
    monkeypatch.setattr("src.controller._now", lambda: 10_000.0)
    ctrl, _github, docker = _controller(write_config, [])
    ctrl.ledger.add(Reservation("powerserver-cici-7", 7, HOMELAB, "light", 700, False))
    docker.list_lanes.return_value = [
        LaneInfo(
            "powerserver-cici-7",
            7,
            "cid-a",
            class_name="light",
            host="powerserver",
            started_at=9_400.0,
        )
    ]

    ctrl.reconcile()
    (lane,) = ctrl.status()["running"]

    assert lane["running_seconds"] == 600


def test_a_restart_does_not_reset_a_running_lanes_age(write_config, monkeypatch) -> None:
    """The whole reason the start time comes from the daemon rather than from a stamp we
    write at spawn: a fresh controller re-adopts a lane that has been up for an hour and
    must say so, not report it as brand new."""
    monkeypatch.setattr("src.controller._now", lambda: 10_000.0)
    ctrl, _github, docker = _controller(write_config, [])  # empty ledger == post-restart
    docker.list_lanes.return_value = [
        LaneInfo(
            "powerserver-cici-7",
            7,
            "cid-a",
            class_name="light",
            host="powerserver",
            started_at=6_400.0,
        )
    ]

    ctrl.reconcile()
    (lane,) = ctrl.status()["running"]

    assert lane["running_seconds"] == 3600


def test_a_lane_with_no_usable_start_time_reports_unknown(write_config, monkeypatch) -> None:
    """Never zero: "0s" on a lane that has been up for an hour is a worse answer than
    admitting the timestamp is missing."""
    monkeypatch.setattr("src.controller._now", lambda: 10_000.0)
    ctrl, _github, docker = _controller(write_config, [])
    docker.list_lanes.return_value = [
        LaneInfo("powerserver-cici-7", 7, "cid-a", class_name="light", host="powerserver")
    ]

    ctrl.reconcile()
    (lane,) = ctrl.status()["running"]

    assert lane["running_seconds"] is None


def test_queue_wait_is_measured_from_the_first_tick_that_deferred_the_job(
    write_config, monkeypatch
) -> None:
    """First-seen wins. Measuring from the latest tick would peg every wait at ~0s no
    matter how long the job had actually been stuck."""
    clock = {"t": 10_000.0}
    monkeypatch.setattr("src.controller._now", lambda: clock["t"])
    job = QueuedJob(job_id=700, repo=HOMELAB, labels=["homelab"])
    ctrl, _github, docker = _controller(write_config, [job])
    _fill_lanes(ctrl, docker)

    ctrl.tick()
    clock["t"] = 10_180.0
    ctrl.tick()

    (queued,) = ctrl.status()["deferred"]
    assert queued["waiting_seconds"] == 180


def test_a_job_that_stops_being_queued_is_dropped_from_the_wait_ledger(
    write_config, monkeypatch
) -> None:
    """The dict is rebuilt from each tick's defers, which is what keeps it from growing
    without bound as jobs come and go."""
    clock = {"t": 10_000.0}
    monkeypatch.setattr("src.controller._now", lambda: clock["t"])
    job = QueuedJob(job_id=700, repo=HOMELAB, labels=["homelab"])
    ctrl, github, docker = _controller(write_config, [job])
    _fill_lanes(ctrl, docker)
    ctrl.tick()
    assert 700 in ctrl._queued_since

    github.list_queued_jobs.side_effect = lambda repo: []  # job left GitHub's queue
    clock["t"] = 10_060.0
    ctrl.tick()

    assert ctrl._queued_since == {}


def test_a_lane_whose_reap_is_refused_is_not_reported_as_booting(write_config, monkeypatch) -> None:
    """A lane past idle_lane_max_seconds whose teardown GitHub keeps refusing must be
    distinguishable in /status from a lane that is genuinely still coming up.

    2026-08-20: lane powerserver-cici-96380840900-658a75 held a 7500 MB node_heavy
    reserve for 2153 s — 3.6x the 600 s "absolute ceiling" — while `make ci-status`
    rendered it as `booting`, identical to a 20-second-old healthy lane. Its job
    (96380840900) was still `queued` with `runner_name: ""` the whole time, so the lane
    was provably jobless. Reading it as "booting" is what cost 40 minutes of forensics:
    the one field that would have named the problem was the one being overwritten.
    """
    ctrl, github, docker, _lanes = _reaper_controller(write_config, [], monkeypatch, idle_for=2153)
    github.delete_runner.return_value = False  # 422: GitHub calls the runner busy

    ctrl.tick()

    docker.remove.assert_not_called()  # the refusal is respected — that part is correct
    lane = next(r for r in ctrl.status()["running"] if r["lane_id"] == "powerserver-cici-7")
    assert lane["state"] != "booting", (
        "a lane 3.6x past the idle cap whose reap was refused is reported as 'booting', "
        "indistinguishable from a healthy lane that started 20 seconds ago"
    )
    assert lane["reap_blocked_reason"] == "deregistration_refused"


def test_a_blocked_idle_reap_is_recorded_in_the_metrics_db(write_config, monkeypatch) -> None:
    """The refusal must land in the events table, not only in an INFO log line.

    The 2026-08-20 lane leak was invisible to `make ci-report` precisely because every
    bail-out path in _reap_idle_lane returns after logging and emits nothing: the DB
    showed an `admit` at 09:35:35 and the next event for that lane 2153 s later, with
    no record of the 200-odd reap attempts in between.
    """
    metrics = MagicMock()
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    ctrl, github, _docker, _lanes = _reaper_controller(write_config, [], monkeypatch, idle_for=2153)
    ctrl.metrics = metrics
    github.delete_runner.return_value = False

    ctrl.tick()

    kinds = [c.kwargs.get("kind") for c in metrics.record_event.call_args_list]
    assert "idle_reap_blocked" in kinds, (
        f"a blocked reap emitted no event (got {kinds}); the leak is unqueryable in ci_bench"
    )
    blocked = next(
        c.kwargs
        for c in metrics.record_event.call_args_list
        if c.kwargs.get("kind") == "idle_reap_blocked"
    )
    assert blocked["reason"] == "deregistration_refused"
    assert blocked["lane_id"] == "powerserver-cici-7"
    assert cfg.idle_lane_max_seconds == 600  # the ceiling this lane blew through
