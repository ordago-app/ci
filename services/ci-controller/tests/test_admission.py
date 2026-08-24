from src.admission import evaluate
from src.config import ControllerConfig
from src.host_stats import HostStats
from src.ledger import Ledger
from src.models import (
    BUDGET_FULL,
    DISK_FULL,
    HOST_PRESSURE,
    KVM_BUSY,
    LANE_CEILING,
    NO_ELIGIBLE_HOST,
    NOT_ALLOWLISTED,
    AdmitDecision,
    DeferDecision,
    QueuedJob,
    Reservation,
)

from tests.conftest import VALID_CONFIG

# Crowded config: lane ceiling of 1 and a tight RAM budget so both gates bind at once.
CROWDED_CONFIG = """\
ram_budget_mb: 1000
max_concurrent_lanes: 1
default_class: light
runner_image: homelab/github-actions-runner:latest
work_dirs:
  ssd: /mnt/ci-ssd/ci-controller
  hdd: /opt/personal/github-actions/ci-controller
classes:
  light:
    ram_mb: 900
    work_disk: ssd
repos:
  - project: homelab
    label_class:
      homelab: light
"""

# Disk-only config: RAM/lane ceilings are slack so the disk-GB budget is the sole gate.
DISK_CONFIG = """\
ram_budget_mb: 100000
max_concurrent_lanes: 50
default_class: light
runner_image: homelab/github-actions-runner:latest
work_dirs:
  ssd: /mnt/ci-ssd/ci-controller
  hdd: /opt/personal/github-actions/ci-controller
disk_budget_gb:
  ssd: 10
classes:
  light:
    ram_mb: 700
    work_disk: ssd
    work_gb: 4
  node:
    ram_mb: 700
    work_disk: hdd
    work_gb: 50
repos:
  - project: homelab
    label_class:
      homelab: light
      node: node
"""


# Two hosts. powerserver holds the whole fleet's class list and the larger RAM budget, so
# it is preferred whenever both hosts pass; powervaro-ci is the light-only relief valve with
# two lanes. Every multi-host scheduling property is expressed against this shape.
TWO_HOST_CONFIG = """\
ram_budget_mb: 4000
max_concurrent_lanes: 1
default_class: light
default_host: powerserver
runner_image: homelab/github-actions-runner:latest
work_dirs:
  ssd: /mnt/ci-ssd/ci-controller
  hdd: /opt/personal/github-actions/ci-controller
classes:
  light:
    ram_mb: 700
    work_disk: ssd
  emulator:
    ram_mb: 2500
    needs_kvm: true
    work_disk: hdd
repos:
  - project: homelab
    label_class:
      homelab: light
      android-e2e: emulator
hosts:
  powerserver:
    docker_endpoint: tcp://docker-socket-proxy:2375
  powervaro-ci:
    docker_endpoint: tcp://100.64.0.2:2375
    ram_budget_mb: 3000
    max_concurrent_lanes: 2
    allowed_classes: [light]
"""

# Neither host will take an emulator lane: the class exists but is allowed nowhere.
NO_EMULATOR_HOST_CONFIG = """\
ram_budget_mb: 4000
max_concurrent_lanes: 4
default_class: light
default_host: powerserver
runner_image: homelab/github-actions-runner:latest
work_dirs:
  ssd: /mnt/ci-ssd/ci-controller
  hdd: /opt/personal/github-actions/ci-controller
classes:
  light:
    ram_mb: 700
    work_disk: ssd
  emulator:
    ram_mb: 2500
    needs_kvm: true
    work_disk: hdd
repos:
  - project: homelab
    label_class:
      homelab: light
      android-e2e: emulator
hosts:
  powerserver:
    docker_endpoint: tcp://docker-socket-proxy:2375
    allowed_classes: [light]
  powervaro-ci:
    docker_endpoint: tcp://100.64.0.2:2375
    allowed_classes: [light]
"""

# Two hosts identical in every respect that selection looks at, declared with the
# alphabetically-later host first so insertion order cannot pass for name order.
TIE_CONFIG = """\
ram_budget_mb: 4000
max_concurrent_lanes: 4
default_class: light
default_host: powerserver
runner_image: homelab/github-actions-runner:latest
work_dirs:
  ssd: /mnt/ci-ssd/ci-controller
  hdd: /opt/personal/github-actions/ci-controller
classes:
  light:
    ram_mb: 700
    work_disk: ssd
repos:
  - project: homelab
    label_class:
      homelab: light
hosts:
  powervaro-ci:
    docker_endpoint: tcp://100.64.0.2:2375
  powerserver:
    docker_endpoint: tcp://docker-socket-proxy:2375
"""

# Equal, small RAM budgets on both hosts with lane ceilings well out of the way, so a
# batch's admissions are gated purely by which host each earlier admission was charged to.
RAM_SPLIT_CONFIG = """\
ram_budget_mb: 1500
max_concurrent_lanes: 10
default_class: light
default_host: powerserver
runner_image: homelab/github-actions-runner:latest
work_dirs:
  ssd: /mnt/ci-ssd/ci-controller
  hdd: /opt/personal/github-actions/ci-controller
classes:
  light:
    ram_mb: 700
    work_disk: ssd
repos:
  - project: homelab
    label_class:
      homelab: light
hosts:
  powerserver:
    docker_endpoint: tcp://docker-socket-proxy:2375
  powervaro-ci:
    docker_endpoint: tcp://100.64.0.2:2375
"""


def _homelab_job(job_id: int, label: str = "homelab") -> QueuedJob:
    return QueuedJob(job_id=job_id, repo="alvaro-francisco-gil/homelab", labels=[label])


def _cfg(write_config) -> ControllerConfig:
    return ControllerConfig.load(write_config(VALID_CONFIG))


def test_admits_light_job(write_config) -> None:
    cfg = _cfg(write_config)
    job = QueuedJob(
        job_id=1, repo="alvaro-francisco-gil/homelab", labels=["self-hosted", "homelab"]
    )
    decisions = evaluate([job], Ledger(), cfg)
    assert isinstance(decisions[0], AdmitDecision)
    assert decisions[0].class_name == "light"
    assert decisions[0].ram_mb == 700


def test_defers_unlisted_repo(write_config) -> None:
    cfg = _cfg(write_config)
    job = QueuedJob(job_id=1, repo="x/y", labels=["self-hosted"])
    decisions = evaluate([job], Ledger(), cfg)
    assert isinstance(decisions[0], DeferDecision)
    assert decisions[0].reason == NOT_ALLOWLISTED


def test_defers_when_budget_full(write_config) -> None:
    cfg = _cfg(write_config)
    led = Ledger()
    # Fill budget to 11800 of 12000 (no room for a 700 light job).
    led.add(Reservation("a", 100, "o/r", "light", 11800, False))
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    decisions = evaluate([job], led, cfg)
    assert isinstance(decisions[0], DeferDecision)
    assert decisions[0].reason == BUDGET_FULL


def test_kvm_exclusivity(write_config) -> None:
    cfg = _cfg(write_config)
    led = Ledger()
    led.add(Reservation("a", 100, "o/r", "emulator", 2500, True))
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/ordago-apps", labels=["android-e2e"])
    decisions = evaluate([job], led, cfg)
    assert isinstance(decisions[0], DeferDecision)
    assert decisions[0].reason == KVM_BUSY


def test_lane_ceiling(write_config) -> None:
    bad = VALID_CONFIG.replace("max_concurrent_lanes: 8", "max_concurrent_lanes: 1")
    cfg = ControllerConfig.load(write_config(bad))
    led = Ledger()
    led.add(Reservation("a", 100, "o/r", "light", 700, False))
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    decisions = evaluate([job], led, cfg)
    assert isinstance(decisions[0], DeferDecision)
    assert decisions[0].reason == LANE_CEILING


def test_already_running_job_deferred(write_config) -> None:
    cfg = _cfg(write_config)
    led = Ledger()
    led.add(Reservation("lane-7", 7, "alvaro-francisco-gil/homelab", "light", 700, False))
    job = QueuedJob(job_id=7, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    decisions = evaluate([job], led, cfg)
    assert isinstance(decisions[0], DeferDecision)
    assert decisions[0].reason == "already_running"


def test_defers_when_disk_budget_full(write_config) -> None:
    cfg = ControllerConfig.load(write_config(DISK_CONFIG))
    led = Ledger()
    # SSD budget 10 GB; 8 GB already reserved on ssd -> no room for a 4 GB light job.
    led.add(Reservation("a", 100, "o/r", "light", 700, False, work_disk="ssd", work_gb=8))
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    decisions = evaluate([job], led, cfg)
    assert isinstance(decisions[0], DeferDecision)
    assert decisions[0].reason == DISK_FULL


def test_disk_budget_per_disk_independent(write_config) -> None:
    # An hdd job is unaffected by a saturated ssd (no hdd budget -> unbounded).
    cfg = ControllerConfig.load(write_config(DISK_CONFIG))
    led = Ledger()
    led.add(Reservation("a", 100, "o/r", "light", 700, False, work_disk="ssd", work_gb=10))
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["node"])
    decisions = evaluate([job], led, cfg)
    assert isinstance(decisions[0], AdmitDecision)
    assert decisions[0].class_name == "node"
    assert decisions[0].work_disk == "hdd"
    assert decisions[0].work_gb == 50


def test_batch_disk_admissions_accumulate(write_config) -> None:
    # SSD budget 10 GB, each light job 4 GB -> two fit (8), third deferred.
    cfg = ControllerConfig.load(write_config(DISK_CONFIG))
    jobs = [
        QueuedJob(job_id=i, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
        for i in (1, 2, 3)
    ]
    decisions = evaluate(jobs, Ledger(), cfg)
    admitted = [d for d in decisions if isinstance(d, AdmitDecision)]
    deferred = [d for d in decisions if isinstance(d, DeferDecision)]
    assert len(admitted) == 2
    assert len(deferred) == 1
    assert deferred[0].reason == DISK_FULL


def test_batch_admissions_accumulate(write_config) -> None:
    # Budget 12000; three emulator-ish heavy 5000 jobs -> only two fit in one batch.
    cfg_text = VALID_CONFIG.replace("ram_mb: 700", "ram_mb: 5000")
    cfg = ControllerConfig.load(write_config(cfg_text))
    jobs = [
        QueuedJob(job_id=i, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
        for i in (1, 2, 3)
    ]
    decisions = evaluate(jobs, Ledger(), cfg)
    admitted = [d for d in decisions if isinstance(d, AdmitDecision)]
    deferred = [d for d in decisions if isinstance(d, DeferDecision)]
    assert len(admitted) == 2
    assert len(deferred) == 1
    assert deferred[0].reason == BUDGET_FULL


def test_guard_defers_on_low_host_ram(write_config) -> None:
    cfg_text = VALID_CONFIG.replace(
        "ram_budget_mb: 12000", "admission_mode: reservation_plus_guard\nram_budget_mb: 12000"
    )
    cfg = ControllerConfig.load(write_config(cfg_text))
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    low = HostStats(mem_available_mb=500, load_1m=1.0)  # below default floor 1500
    decisions = evaluate([job], Ledger(), cfg, {"powerserver": low})
    assert isinstance(decisions[0], DeferDecision)
    assert decisions[0].reason == HOST_PRESSURE


def test_guard_admits_when_host_has_headroom(write_config) -> None:
    cfg_text = VALID_CONFIG.replace(
        "ram_budget_mb: 12000", "admission_mode: reservation_plus_guard\nram_budget_mb: 12000"
    )
    cfg = ControllerConfig.load(write_config(cfg_text))
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    ok = HostStats(mem_available_mb=8000, load_1m=1.0)
    assert isinstance(evaluate([job], Ledger(), cfg, {"powerserver": ok})[0], AdmitDecision)


def test_reservation_mode_ignores_host_stats(write_config) -> None:
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))  # default reservation
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    low = HostStats(mem_available_mb=100, load_1m=99.0)
    assert isinstance(evaluate([job], Ledger(), cfg, {"powerserver": low})[0], AdmitDecision)


def test_defer_records_every_binding_gate_not_just_the_first(write_config) -> None:
    """lane_ceiling must not mask budget_full: both gates bind, both are recorded."""
    config = ControllerConfig.load(write_config(CROWDED_CONFIG))
    ledger = Ledger()
    ledger.add(
        Reservation(
            lane_id="lane-1",
            spawned_for_job_id=1,
            repo="alvaro-francisco-gil/homelab",
            class_name="light",
            ram_mb=900,
            needs_kvm=False,
        )
    )
    job = QueuedJob(job_id=2, repo="alvaro-francisco-gil/homelab", labels=["homelab"])

    (decision,) = evaluate([job], ledger, config)

    assert isinstance(decision, DeferDecision)
    assert decision.reasons == (LANE_CEILING, BUDGET_FULL)
    assert decision.reason == LANE_CEILING  # primary stays first-gate, for the historical rows


def test_single_binding_gate_yields_one_reason(write_config) -> None:
    # SSD budget 10 GB; 8 GB already reserved on ssd -> no room for a 4 GB light job.
    # Lanes/RAM/kvm are all slack, so disk_full is the only gate that binds.
    config = ControllerConfig.load(write_config(DISK_CONFIG))
    ledger = Ledger()
    ledger.add(Reservation("a", 100, "o/r", "light", 700, False, work_disk="ssd", work_gb=8))
    job = QueuedJob(job_id=3, repo="alvaro-francisco-gil/homelab", labels=["homelab"])

    (decision,) = evaluate([job], ledger, config)

    assert isinstance(decision, DeferDecision)
    assert decision.reasons == (DISK_FULL,)


def test_not_allowlisted_short_circuits_before_capacity_gates(write_config) -> None:
    """A job we may not run has no capacity verdict to report."""
    config = ControllerConfig.load(write_config(CROWDED_CONFIG))
    job = QueuedJob(job_id=4, repo="someone/else", labels=["homelab"])

    (decision,) = evaluate([job], Ledger(), config)

    assert isinstance(decision, DeferDecision)
    assert decision.reasons == (NOT_ALLOWLISTED,)


def test_a_job_deferred_by_the_primary_lands_on_the_second_host(write_config) -> None:
    """The whole point of Phase 3: lane_ceiling on one host is not lane_ceiling on the fleet."""
    config = ControllerConfig.load(write_config(TWO_HOST_CONFIG))
    ledger = Ledger()
    ledger.add(
        Reservation(
            lane_id="lane-1",
            spawned_for_job_id=1,
            repo="alvaro-francisco-gil/homelab",
            class_name="light",
            ram_mb=700,
            needs_kvm=False,
            host="powerserver",
        )
    )

    (decision,) = evaluate([_homelab_job(2)], ledger, config)

    assert isinstance(decision, AdmitDecision)
    assert decision.host == "powervaro-ci"
    assert decision.class_name == "light"


def test_the_emulator_class_is_never_scheduled_off_its_allowed_host(write_config) -> None:
    """allowed_classes is a hard filter, not a preference."""
    config = ControllerConfig.load(write_config(TWO_HOST_CONFIG))
    free = evaluate([_homelab_job(1, "android-e2e")], Ledger(), config)
    assert isinstance(free[0], AdmitDecision)
    assert free[0].host == "powerserver"

    ledger = Ledger()
    ledger.add(
        Reservation(
            lane_id="lane-1",
            spawned_for_job_id=1,
            repo="alvaro-francisco-gil/homelab",
            class_name="light",
            ram_mb=700,
            needs_kvm=False,
            host="powerserver",
        )
    )

    (decision,) = evaluate([_homelab_job(2, "android-e2e")], ledger, config)

    # powervaro-ci has a free lane, but emulator is not in its allowed_classes.
    assert isinstance(decision, DeferDecision)
    assert decision.reasons == (LANE_CEILING,)


def test_no_eligible_host_when_every_host_disallows_the_class(write_config) -> None:
    config = ControllerConfig.load(write_config(NO_EMULATOR_HOST_CONFIG))

    (decision,) = evaluate([_homelab_job(1, "android-e2e")], Ledger(), config)

    assert isinstance(decision, DeferDecision)
    assert decision.reasons == (NO_ELIGIBLE_HOST,)


def test_an_unhealthy_host_is_skipped_for_admission(write_config) -> None:
    """healthy={"powerserver"} => nothing is admitted to powervaro-ci."""
    config = ControllerConfig.load(write_config(TWO_HOST_CONFIG))
    ledger = Ledger()
    ledger.add(
        Reservation(
            lane_id="lane-1",
            spawned_for_job_id=1,
            repo="alvaro-francisco-gil/homelab",
            class_name="light",
            ram_mb=700,
            needs_kvm=False,
            host="powerserver",
        )
    )

    (decision,) = evaluate([_homelab_job(2)], ledger, config, healthy={"powerserver"})

    assert isinstance(decision, DeferDecision)
    assert decision.reasons == (LANE_CEILING,)


def test_every_eligible_host_unhealthy_yields_no_eligible_host(write_config) -> None:
    config = ControllerConfig.load(write_config(TWO_HOST_CONFIG))

    (decision,) = evaluate([_homelab_job(1)], Ledger(), config, healthy=set())

    assert isinstance(decision, DeferDecision)
    assert decision.reasons == (NO_ELIGIBLE_HOST,)


def test_reasons_match_the_closest_host_not_the_first(write_config) -> None:
    """Host A is over lane_ceiling AND budget_full; host B only lane_ceiling.

    The recorded reasons must be ("lane_ceiling",) — B is closer.
    """
    config = ControllerConfig.load(write_config(TWO_HOST_CONFIG))
    ledger = Ledger()
    # powerserver: its single lane is taken and 3500 of 4000 MB is committed.
    ledger.add(
        Reservation(
            lane_id="lane-ps",
            spawned_for_job_id=1,
            repo="alvaro-francisco-gil/homelab",
            class_name="light",
            ram_mb=3500,
            needs_kvm=False,
            host="powerserver",
        )
    )
    # powervaro-ci: both lanes taken but only 1400 of 3000 MB committed.
    for index in (2, 3):
        ledger.add(
            Reservation(
                lane_id=f"lane-pv-{index}",
                spawned_for_job_id=index,
                repo="alvaro-francisco-gil/homelab",
                class_name="light",
                ram_mb=700,
                needs_kvm=False,
                host="powervaro-ci",
            )
        )

    (decision,) = evaluate([_homelab_job(4)], ledger, config)

    assert isinstance(decision, DeferDecision)
    assert decision.reasons == (LANE_CEILING,)
    assert decision.reason == LANE_CEILING


def test_single_host_config_produces_identical_decisions_to_the_pre_hosts_behaviour(
    write_config,
) -> None:
    """Equivalence: run the existing CROWDED_CONFIG fixtures and assert the same
    (kind, reasons) sequence the pre-migration suite asserted."""
    crowded = ControllerConfig.load(write_config(CROWDED_CONFIG))
    crowded_jobs = [
        _homelab_job(1),
        _homelab_job(2),
        QueuedJob(job_id=3, repo="someone/else", labels=["homelab"]),
    ]

    crowded_decisions = evaluate(crowded_jobs, Ledger(), crowded)

    assert [_shape(d) for d in crowded_decisions] == [
        ("admit", ("light",)),
        ("defer", (LANE_CEILING, BUDGET_FULL)),
        ("defer", (NOT_ALLOWLISTED,)),
    ]
    assert all(d.host == "powerserver" for d in crowded_decisions if isinstance(d, AdmitDecision))

    valid = ControllerConfig.load(write_config(VALID_CONFIG))
    ledger = Ledger()
    ledger.add(Reservation("lane-9", 9, "alvaro-francisco-gil/homelab", "light", 700, False))
    valid_jobs = [
        _homelab_job(9),
        _homelab_job(10),
        QueuedJob(job_id=11, repo="alvaro-francisco-gil/ordago-apps", labels=["android-e2e"]),
    ]

    valid_decisions = evaluate(valid_jobs, ledger, valid)

    assert [_shape(d) for d in valid_decisions] == [
        ("defer", ("already_running",)),
        ("admit", ("light",)),
        ("admit", ("emulator",)),
    ]


def test_ties_break_by_host_name_ascending(write_config) -> None:
    config = ControllerConfig.load(write_config(TIE_CONFIG))

    (decision,) = evaluate([_homelab_job(1)], Ledger(), config)

    assert isinstance(decision, AdmitDecision)
    assert decision.host == "powerserver"


def test_batch_lane_accumulation_is_charged_per_host(write_config) -> None:
    """Two admissions to powervaro-ci must charge powervaro-ci twice, not the fleet twice."""
    config = ControllerConfig.load(write_config(TWO_HOST_CONFIG))
    jobs = [_homelab_job(i) for i in (1, 2, 3, 4)]

    decisions = evaluate(jobs, Ledger(), config)

    assert [_shape(d) for d in decisions] == [
        ("admit", ("light",)),
        ("admit", ("light",)),
        ("admit", ("light",)),
        ("defer", (LANE_CEILING,)),
    ]
    assert [d.host for d in decisions if isinstance(d, AdmitDecision)] == [
        "powerserver",
        "powervaro-ci",
        "powervaro-ci",
    ]


def test_batch_ram_accumulation_is_charged_per_host(write_config) -> None:
    """Each host's RAM budget is spent only by the jobs actually placed on it."""
    config = ControllerConfig.load(write_config(RAM_SPLIT_CONFIG))
    jobs = [_homelab_job(i) for i in (1, 2, 3, 4, 5)]

    decisions = evaluate(jobs, Ledger(), config)

    # 1500 MB per host, 700 MB per lane: two lanes per host, then both budgets are full.
    assert [_shape(d) for d in decisions] == [
        ("admit", ("light",)),
        ("admit", ("light",)),
        ("admit", ("light",)),
        ("admit", ("light",)),
        ("defer", (BUDGET_FULL,)),
    ]
    assert [d.host for d in decisions if isinstance(d, AdmitDecision)] == [
        "powerserver",
        "powervaro-ci",
        "powerserver",
        "powervaro-ci",
    ]


def test_host_stats_are_read_per_host(write_config) -> None:
    """A host under memory pressure is skipped; a host with no live stats cannot trip
    host_pressure at all, which is the pre-migration `host_stats=None` semantics."""
    guarded = TWO_HOST_CONFIG.replace(
        "ram_budget_mb: 4000\n", "admission_mode: reservation_plus_guard\nram_budget_mb: 4000\n", 1
    )
    config = ControllerConfig.load(write_config(guarded))
    stats = {"powerserver": HostStats(mem_available_mb=500, load_1m=1.0)}

    (decision,) = evaluate([_homelab_job(1)], Ledger(), config, stats)

    assert isinstance(decision, AdmitDecision)
    assert decision.host == "powervaro-ci"


def test_host_pressure_on_every_host_defers_with_host_pressure(write_config) -> None:
    guarded = TWO_HOST_CONFIG.replace(
        "ram_budget_mb: 4000\n", "admission_mode: reservation_plus_guard\nram_budget_mb: 4000\n", 1
    )
    config = ControllerConfig.load(write_config(guarded))
    stats = {
        "powerserver": HostStats(mem_available_mb=500, load_1m=1.0),
        "powervaro-ci": HostStats(mem_available_mb=400, load_1m=1.0),
    }

    (decision,) = evaluate([_homelab_job(1)], Ledger(), config, stats)

    assert isinstance(decision, DeferDecision)
    assert decision.reasons == (HOST_PRESSURE,)


def test_a_disabled_host_is_never_scheduled_on(write_config) -> None:
    disabled = TWO_HOST_CONFIG.replace(
        "    ram_budget_mb: 3000\n", "    enabled: false\n    ram_budget_mb: 3000\n", 1
    )
    config = ControllerConfig.load(write_config(disabled))
    ledger = Ledger()
    ledger.add(
        Reservation(
            lane_id="lane-1",
            spawned_for_job_id=1,
            repo="alvaro-francisco-gil/homelab",
            class_name="light",
            ram_mb=700,
            needs_kvm=False,
            host="powerserver",
        )
    )

    (decision,) = evaluate([_homelab_job(2)], ledger, config)

    assert isinstance(decision, DeferDecision)
    assert decision.reasons == (LANE_CEILING,)


def _shape(decision) -> tuple[str, tuple[str, ...]]:
    """(kind, payload) of a decision: the class name for admits, the reasons for defers."""
    if isinstance(decision, AdmitDecision):
        return ("admit", (decision.class_name,))
    return ("defer", decision.reasons)


def test_a_capacity_defer_names_the_class_the_job_would_have_run_as(write_config) -> None:
    """Without this the queue is a list of opaque job ids: six waiting node_heavy jobs
    look exactly like six light ones an idle light-only host could have absorbed."""
    config = ControllerConfig.load(write_config(CROWDED_CONFIG))
    ledger = Ledger()
    ledger.add(
        Reservation(
            lane_id="lane-1",
            spawned_for_job_id=1,
            repo="alvaro-francisco-gil/homelab",
            class_name="light",
            ram_mb=900,
            needs_kvm=False,
        )
    )

    (decision,) = evaluate([_homelab_job(2)], ledger, config)

    assert isinstance(decision, DeferDecision)
    assert decision.reasons == (LANE_CEILING, BUDGET_FULL)  # CROWDED_CONFIG binds both
    assert decision.class_name == "light"


def test_no_eligible_host_still_names_the_class(write_config) -> None:
    """The defer that most needs its class: 'nothing will run this kind of job' is only
    actionable if you know which kind."""
    config = ControllerConfig.load(write_config(NO_EMULATOR_HOST_CONFIG))

    (decision,) = evaluate([_homelab_job(1, "android-e2e")], Ledger(), config)

    assert isinstance(decision, DeferDecision)
    assert decision.class_name == "emulator"


def test_a_job_with_no_class_to_name_reports_none(write_config) -> None:
    """not_allowlisted has no mapped label, so there is no class — the field must stay
    None rather than being filled with the default class, which would be a fiction."""
    config = ControllerConfig.load(write_config(CROWDED_CONFIG))
    job = QueuedJob(job_id=4, repo="someone/else", labels=["homelab"])

    (decision,) = evaluate([job], Ledger(), config)

    assert isinstance(decision, DeferDecision)
    assert decision.reasons == (NOT_ALLOWLISTED,)
    assert decision.class_name is None
