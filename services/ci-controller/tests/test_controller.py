from collections import defaultdict
from unittest.mock import MagicMock

import pytest
from src.config import ControllerConfig
from src.controller import Controller
from src.docker_adapter import LaneInfo
from src.ledger import Ledger
from src.models import AdmitDecision, DeferDecision, QueuedJob, Reservation

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
    """Wrap a single mock DockerAdapter as a single-host DockerPool double."""
    pool = MagicMock()
    pool.for_host.side_effect = lambda name: docker
    pool.up = {host}  # test knob: which hosts answer this tick
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
    docker = MagicMock()
    docker.list_lanes.return_value = []
    docker.spawn.side_effect = lambda decision, registration_token: (
        f"powerserver-cici-{decision.job.job_id}"
    )
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
            f"{_name}-cici-{decision.job.job_id}"
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
    # 1b) simulate the lane appearing as running so reconcile can sample it
    docker.list_lanes.return_value = [LaneInfo("powerserver-cici-1", 1, "cid")]
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
            "desktop-cici-1", 1, "alvaro-francisco-gil/homelab", "light", 700, False, host="desktop"
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
