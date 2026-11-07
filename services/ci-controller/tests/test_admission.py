import pytest
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
)
from src.models import (
    Reservation as _Reservation,
)

from tests.conftest import VALID_CONFIG

# Test shim, not production behaviour. These tests are all about lanes on ONE
# host, so an explicit `host=` would be the same literal dozens of times and
# would say nothing. `Reservation.host` stopped defaulting to a real machine
# when the platform was prepared for a second operator — see
# tests/test_no_operator_defaults.py — so the convenience lives here, in the
# fixture, instead of in shipped code. Tests that are about a NAMED other host
# pass `host=` explicitly and setdefault leaves them alone.
THIS_HOST = "powerserver"


def Reservation(*args, **kwargs) -> _Reservation:
    kwargs.setdefault("host", THIS_HOST)
    return _Reservation(*args, **kwargs)


# Crowded config: lane ceiling of 1 and a tight RAM budget so both gates bind at once.
CROWDED_CONFIG = """\
ram_budget_mb: 1000
max_concurrent_lanes: 1
default_host: powerserver
default_class: light
runner_image: homelab/github-actions-runner:latest
work_dirs:
  ssd: /mnt/ci-ssd/ci-controller
  hdd: /opt/personal/github-actions/ci-controller
classes:
  light:
    ram_mb: 900
    work_disk: ssd
review_admission:
  cheap_classes: []
  session_host: powerserver
  session_ram_mb: 600
repos:
  - project: homelab
    label_class:
      homelab: light
"""

# Two non-KVM classes of very different size, and a budget that fits the big one plus
# exactly one small one. Arrival order alone decides whether the big one ever runs.
MIXED_SIZE_CONFIG = """\
ram_budget_mb: 5000
max_concurrent_lanes: 8
default_host: powerserver
default_class: light
runner_image: homelab/github-actions-runner:latest
work_dirs:
  ssd: /mnt/ci-ssd/ci-controller
  hdd: /opt/personal/github-actions/ci-controller
classes:
  heavy:
    ram_mb: 4000
    work_disk: ssd
  light:
    ram_mb: 700
    work_disk: ssd
review_admission:
  cheap_classes: []
  session_host: powerserver
  session_ram_mb: 600
repos:
  - project: homelab
    label_class:
      homelab-heavy: heavy
      homelab: light
"""

# Disk-only config: RAM/lane ceilings are slack so the disk-GB budget is the sole gate.
DISK_CONFIG = """\
ram_budget_mb: 100000
max_concurrent_lanes: 50
default_host: powerserver
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
review_admission:
  cheap_classes: []
  session_host: powerserver
  session_ram_mb: 600
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
review_admission:
  cheap_classes: []
  session_host: powerserver
  session_ram_mb: 600
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
review_admission:
  cheap_classes: []
  session_host: powerserver
  session_ram_mb: 600
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
review_admission:
  cheap_classes: []
  session_host: powerserver
  session_ram_mb: 600
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
review_admission:
  cheap_classes: []
  session_host: powerserver
  session_ram_mb: 600
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
    job = QueuedJob(job_id=1, repo="ordago-app/ordago-apps", labels=["android-e2e"])
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


# powervaro-ci offers MORE headroom than powerserver, so without a rank it would win
# every job; ranking powerserver first must override that and nothing else.
RANKED_HOSTS_CONFIG = """\
ram_budget_mb: 4000
max_concurrent_lanes: 2
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
review_admission:
  cheap_classes: []
  session_host: powerserver
  session_ram_mb: 600
repos:
  - project: homelab
    label_class:
      homelab: light
hosts:
  powerserver:
    docker_endpoint: tcp://docker-socket-proxy:2375
  powervaro-ci:
    docker_endpoint: tcp://100.64.0.2:2375
    ram_budget_mb: 20000
    max_concurrent_lanes: 10
    placement_rank: 1
"""


def test_the_lowest_ranked_host_wins_over_one_with_more_headroom(write_config) -> None:
    config = ControllerConfig.load(write_config(RANKED_HOSTS_CONFIG))

    (decision,) = evaluate([_homelab_job(1)], Ledger(), config)

    assert isinstance(decision, AdmitDecision)
    assert decision.host == "powerserver"


def test_a_job_the_preferred_host_cannot_take_spills_to_the_next_rank(write_config) -> None:
    """Rank is a preference, never a gate: a full first host must not leave a job queued
    while a later one has room. The batch fills powerserver's 2 lanes, then spills."""
    config = ControllerConfig.load(write_config(RANKED_HOSTS_CONFIG))
    jobs = [_homelab_job(job_id) for job_id in (1, 2, 3, 4)]

    decisions = evaluate(jobs, Ledger(), config)

    assert all(isinstance(d, AdmitDecision) for d in decisions)
    assert [d.host for d in decisions] == [
        "powerserver",
        "powerserver",
        "powervaro-ci",
        "powervaro-ci",
    ]


def test_an_unset_rank_keeps_most_headroom_placement(write_config) -> None:
    """Every host predates placement_rank; omitting it everywhere must not move work."""
    config = ControllerConfig.load(
        write_config(RANKED_HOSTS_CONFIG.replace("    placement_rank: 1\n", ""))
    )

    (decision,) = evaluate([_homelab_job(1)], Ledger(), config)

    assert isinstance(decision, AdmitDecision)
    assert decision.host == "powervaro-ci"


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


def test_a_defer_names_every_hosts_gates_not_only_the_closest(write_config) -> None:
    """Reporting only the closest host hid why a roomier one refused: on 2026-11-07 node
    jobs read `budget_full` (the elastic host was 42 MB short) while powerserver, its
    reserve half empty, was actually holding them on host_pressure."""
    config = ControllerConfig.load(write_config(TWO_HOST_CONFIG))
    ledger = Ledger()
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
    assert decision.gates_by_host == {
        "powerserver": (LANE_CEILING, BUDGET_FULL),
        "powervaro-ci": (LANE_CEILING,),
    }


def test_a_defer_that_is_no_capacity_verdict_names_no_host_gates(write_config) -> None:
    config = ControllerConfig.load(write_config(TWO_HOST_CONFIG))

    (decision,) = evaluate([_homelab_job(1)], Ledger(), config, healthy=set())

    assert isinstance(decision, DeferDecision)
    assert decision.gates_by_host == {}


def test_single_host_config_produces_identical_decisions_to_the_pre_hosts_behaviour(
    write_config,
) -> None:
    """Equivalence: run the existing CROWDED_CONFIG fixtures and assert the same
    (kind, reasons) verdict per job the pre-migration suite asserted.

    Keyed by job id, not by position: decisions come back largest-reserve-first, which
    is the ordering `test_the_biggest_job_is_admitted_ahead_of_smaller_ones_queued_before_it`
    owns. What must not drift is the verdict each job gets."""
    crowded = ControllerConfig.load(write_config(CROWDED_CONFIG))
    crowded_jobs = [
        _homelab_job(1),
        _homelab_job(2),
        QueuedJob(job_id=3, repo="someone/else", labels=["homelab"]),
    ]

    crowded_decisions = evaluate(crowded_jobs, Ledger(), crowded)

    assert {d.job.job_id: _shape(d) for d in crowded_decisions} == {
        1: ("admit", ("light",)),
        2: ("defer", (LANE_CEILING, BUDGET_FULL)),
        3: ("defer", (NOT_ALLOWLISTED,)),
    }
    assert all(d.host == "powerserver" for d in crowded_decisions if isinstance(d, AdmitDecision))

    valid = ControllerConfig.load(write_config(VALID_CONFIG))
    ledger = Ledger()
    ledger.add(Reservation("lane-9", 9, "alvaro-francisco-gil/homelab", "light", 700, False))
    valid_jobs = [
        _homelab_job(9),
        _homelab_job(10),
        QueuedJob(job_id=11, repo="ordago-app/ordago-apps", labels=["android-e2e"]),
    ]

    valid_decisions = evaluate(valid_jobs, ledger, valid)

    assert {d.job.job_id: _shape(d) for d in valid_decisions} == {
        9: ("defer", ("already_running",)),
        10: ("admit", ("light",)),
        11: ("admit", ("emulator",)),
    }


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


# Two hosts that differ ONLY in which org may place work on them. Both allow the
# `light` class and both have ample budget, so any difference in the outcome is
# `allowed_orgs` and nothing else.
ORG_SPLIT_CONFIG = """\
ram_budget_mb: 4000
max_concurrent_lanes: 4
default_class: light
default_host: aaa-personal
runner_image: homelab/github-actions-runner:latest
work_dirs:
  ssd: /mnt/ci-ssd/ci-controller
  hdd: /opt/personal/github-actions/ci-controller
classes:
  light:
    ram_mb: 700
    work_disk: ssd
review_admission:
  cheap_classes: []
  session_host: aaa-personal
  session_ram_mb: 600
repos:
  - project: homelab
    label_class:
      homelab: light
  - project: ordago-apps
    label_class:
      ordago-ci: light
hosts:
  aaa-personal:
    docker_endpoint: tcp://100.64.0.4:2375
    allowed_orgs: [alvaro-francisco-gil]
  zzz-ordago:
    docker_endpoint: tcp://100.64.0.3:2375
    allowed_orgs: [ordago-app]
"""


def test_a_host_that_excludes_the_jobs_org_is_not_eligible(write_config):
    """allowed_orgs is the scheduling half of the lane-host trust boundary. A host
    listing only [ordago-app] must never be offered a personal job, and vice versa.
    The SAME fact generates the tailnet ACL, so placement can never grant reach the
    fabric refuses -- see docs/plans/ideas/federated-ci-pool.md decision 6."""
    config = ControllerConfig.load(write_config(ORG_SPLIT_CONFIG))
    ledger = Ledger()

    ordago_job = QueuedJob(job_id=1, repo="ordago-app/ordago-apps", labels=["ordago-ci"])
    personal_job = QueuedJob(job_id=2, repo="alvaro-francisco-gil/homelab", labels=["homelab"])

    # Named so the WRONG host wins every tie-break without the filter: equal
    # headroom is broken by name ascending, so a single ordago job would land on
    # aaa-personal. Landing on zzz-ordago is therefore evidence of the filter and
    # not of the tie-break.
    decisions = evaluate([ordago_job], ledger, config, {}, None)
    assert isinstance(decisions[0], AdmitDecision)
    assert decisions[0].host == "zzz-ordago"

    decisions = evaluate([personal_job], ledger, config, {}, None)
    assert isinstance(decisions[0], AdmitDecision)
    assert decisions[0].host == "aaa-personal"


def test_a_job_no_host_admits_the_org_of_defers_as_no_eligible_host(write_config):
    """An org nobody accepts is an eligibility verdict, not a capacity one -- the
    fleet being empty would not help."""
    config = ControllerConfig.load(write_config(ORG_SPLIT_CONFIG))
    ledger = Ledger()
    job = QueuedJob(job_id=1, repo="ordago-app/ordago-apps", labels=["ordago-ci"])

    # Take ordago-host out of the healthy set: the only host that admits this org.
    decisions = evaluate([job], ledger, config, {}, {"aaa-personal"})

    assert isinstance(decisions[0], DeferDecision)
    assert decisions[0].reasons == (NO_ELIGIBLE_HOST,)


def test_omitting_allowed_orgs_means_every_org(write_config):
    """Absent config must not silently deny -- every host predates this field."""
    config = ControllerConfig.load(write_config(TWO_HOST_CONFIG))
    ledger = Ledger()
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])

    decisions = evaluate([job], ledger, config, {}, None)

    assert isinstance(decisions[0], AdmitDecision)
    assert decisions[0].host == "powerserver"


def _homelab(job_id: int, label: str) -> QueuedJob:
    return QueuedJob(job_id=job_id, repo="alvaro-francisco-gil/homelab", labels=[label])


def test_the_biggest_job_is_admitted_ahead_of_smaller_ones_queued_before_it(
    write_config,
) -> None:
    # Budget 5000. In arrival order the two lights (700 each) go first and heavy (4000)
    # no longer fits; largest-first admits heavy and one light, and defers the other.
    cfg = ControllerConfig.load(write_config(MIXED_SIZE_CONFIG))
    jobs = [_homelab(1, "homelab"), _homelab(2, "homelab"), _homelab(3, "homelab-heavy")]

    decisions = evaluate(jobs, Ledger(), cfg)

    admitted = {d.job.job_id: d.class_name for d in decisions if isinstance(d, AdmitDecision)}
    deferred = [d for d in decisions if isinstance(d, DeferDecision)]
    assert admitted == {3: "heavy", 1: "light"}
    assert [d.job.job_id for d in deferred] == [2]
    assert deferred[0].reasons == (BUDGET_FULL,)


def test_jobs_reserving_the_same_amount_keep_arrival_order(write_config) -> None:
    # Stability: with one light-sized slot left after heavy, the FIRST light queued wins.
    cfg = ControllerConfig.load(write_config(MIXED_SIZE_CONFIG))
    jobs = [_homelab(3, "homelab-heavy"), _homelab(1, "homelab"), _homelab(2, "homelab")]

    decisions = evaluate(jobs, Ledger(), cfg)

    admitted = [d.job.job_id for d in decisions if isinstance(d, AdmitDecision)]
    assert admitted == [3, 1]


def test_an_unservable_job_is_ordered_last_without_changing_any_verdict(write_config) -> None:
    # A job whose repo is not allowlisted reserves nothing, so it must neither consume
    # budget nor push a real job behind it — it just reports not_allowlisted.
    cfg = ControllerConfig.load(write_config(MIXED_SIZE_CONFIG))
    jobs = [
        QueuedJob(job_id=9, repo="alvaro-francisco-gil/not-served", labels=["homelab-heavy"]),
        _homelab(3, "homelab-heavy"),
        _homelab(1, "homelab"),
    ]

    decisions = evaluate(jobs, Ledger(), cfg)

    admitted = [d.job.job_id for d in decisions if isinstance(d, AdmitDecision)]
    unservable = next(d for d in decisions if d.job.job_id == 9)
    assert admitted == [3, 1]
    assert isinstance(unservable, DeferDecision)
    assert unservable.reasons == (NOT_ALLOWLISTED,)


def _load_guarded(write_config) -> ControllerConfig:
    return ControllerConfig.load(
        write_config(
            VALID_CONFIG.replace(
                "ram_budget_mb: 12000",
                "admission_mode: reservation_plus_guard\nhost_load_ceiling: 16.0\n"
                "ram_budget_mb: 12000",
            )
        )
    )


def test_load_that_is_cpu_contention_defers(write_config) -> None:
    cfg = _load_guarded(write_config)
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    oversubscribed = HostStats(mem_available_mb=8000, load_1m=40.0, cpu_pressure_pct=95.0)
    decisions = evaluate([job], Ledger(), cfg, {"powerserver": oversubscribed})
    assert isinstance(decisions[0], DeferDecision)
    assert decisions[0].reason == HOST_PRESSURE


def test_load_that_is_only_tasks_blocked_on_io_admits(write_config) -> None:
    """2026-10-20: one container thrashing its own memory limit swapped onto the HDD.
    Its tasks blocked in IO pushed load_1m to 20-50 for five hours while the CPUs
    were 5-8 % busy and RAM was 9 GB free, and the load gate deferred every CI job.
    Load average counts tasks in uninterruptible IO wait; the ceiling exists to stop
    CPU oversubscription, so load only counts while the CPUs are contended."""
    cfg = _load_guarded(write_config)
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    io_bound = HostStats(mem_available_mb=9000, load_1m=40.0, cpu_pressure_pct=0.2)
    assert isinstance(evaluate([job], Ledger(), cfg, {"powerserver": io_bound})[0], AdmitDecision)


def test_load_without_a_cpu_pressure_reading_still_defers(write_config) -> None:
    # No PSI (kernel without it, or the file unreadable): the gate keeps its old
    # meaning rather than switching itself off.
    cfg = _load_guarded(write_config)
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    blind = HostStats(mem_available_mb=8000, load_1m=40.0, cpu_pressure_pct=None)
    decisions = evaluate([job], Ledger(), cfg, {"powerserver": blind})
    assert isinstance(decisions[0], DeferDecision)
    assert decisions[0].reason == HOST_PRESSURE


def test_the_ram_floor_does_not_depend_on_cpu_pressure(write_config) -> None:
    cfg = _load_guarded(write_config)
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    low_ram = HostStats(mem_available_mb=500, load_1m=1.0, cpu_pressure_pct=0.0)
    decisions = evaluate([job], Ledger(), cfg, {"powerserver": low_ram})
    assert isinstance(decisions[0], DeferDecision)
    assert decisions[0].reason == HOST_PRESSURE


@pytest.mark.parametrize(
    ("load", "pressure", "deferred"),
    [
        (40.0, 49.9, False),  # just under the CPU-pressure threshold
        (40.0, 50.0, True),  # at it
        (16.0, 95.0, False),  # at the load ceiling, which is a strict bound
        (16.1, 95.0, True),
    ],
)
def test_load_gate_boundaries(write_config, load, pressure, deferred) -> None:
    cfg = _load_guarded(write_config)
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    stats = HostStats(mem_available_mb=8000, load_1m=load, cpu_pressure_pct=pressure)
    decision = evaluate([job], Ledger(), cfg, {"powerserver": stats})[0]
    assert isinstance(decision, DeferDecision) is deferred


def test_a_disabled_load_gate_ignores_load_and_cpu_pressure(write_config) -> None:
    cfg = ControllerConfig.load(
        write_config(
            VALID_CONFIG.replace(
                "ram_budget_mb: 12000",
                "admission_mode: reservation_plus_guard\nhost_load_ceiling: 0.0\n"
                "ram_budget_mb: 12000",
            )
        )
    )
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    stats = HostStats(mem_available_mb=8000, load_1m=99.0, cpu_pressure_pct=100.0)
    assert isinstance(evaluate([job], Ledger(), cfg, {"powerserver": stats})[0], AdmitDecision)


# ─── Elastic hosts: the budget follows the host's own offer ──────────────────────────
# Only powervaro-ci is healthy below, so every verdict is that host's alone.
ELASTIC_HOST_CONFIG = TWO_HOST_CONFIG.replace(
    "    ram_budget_mb: 3000\n",
    "    ram_budget_mb: 3000\n    ram_offer_reserve_mb: 1000\n",
)

DESKTOP_ONLY = {"powervaro-ci"}


def test_an_elastic_host_is_budgeted_by_its_offer_below_the_ceiling(write_config) -> None:
    config = ControllerConfig.load(write_config(ELASTIC_HOST_CONFIG))

    # MemTotal 1500 - reserve 1000 = 500 MB on offer: a 700 MB light lane does not fit.
    (decision,) = evaluate(
        [_homelab_job(2)], Ledger(), config, healthy=DESKTOP_ONLY, ram_offers={"powervaro-ci": 500}
    )

    assert isinstance(decision, DeferDecision)
    assert BUDGET_FULL in decision.reasons


def test_an_elastic_host_admits_once_its_offer_grows(write_config) -> None:
    config = ControllerConfig.load(write_config(ELASTIC_HOST_CONFIG))

    (decision,) = evaluate(
        [_homelab_job(2)], Ledger(), config, healthy=DESKTOP_ONLY, ram_offers={"powervaro-ci": 800}
    )

    assert isinstance(decision, AdmitDecision)
    assert decision.host == "powervaro-ci"


def test_an_offer_above_the_ceiling_is_capped_by_ram_budget_mb(write_config) -> None:
    config = ControllerConfig.load(write_config(ELASTIC_HOST_CONFIG))
    ledger = Ledger()
    ledger.add(
        Reservation(
            "p1", 3, "alvaro-francisco-gil/homelab", "light", 2800, False, host="powervaro-ci"
        )
    )

    # 2800 + 700 > the 3000 ceiling, however large the offer.
    (decision,) = evaluate(
        [_homelab_job(2)], ledger, config, healthy=DESKTOP_ONLY, ram_offers={"powervaro-ci": 30000}
    )

    assert isinstance(decision, DeferDecision)
    assert BUDGET_FULL in decision.reasons


def test_an_elastic_host_without_an_offer_admits_nothing(write_config) -> None:
    """No reading must never mean "use the ceiling": that is the silent fallback that
    would over-admit onto a guest the balloon has shrunk."""
    config = ControllerConfig.load(write_config(ELASTIC_HOST_CONFIG))

    (decision,) = evaluate([_homelab_job(2)], Ledger(), config, healthy=DESKTOP_ONLY, ram_offers={})

    assert isinstance(decision, DeferDecision)
    assert BUDGET_FULL in decision.reasons


def test_a_host_without_a_reserve_ignores_offers(write_config) -> None:
    config = ControllerConfig.load(write_config(TWO_HOST_CONFIG))

    (decision,) = evaluate(
        [_homelab_job(2)], Ledger(), config, healthy=DESKTOP_ONLY, ram_offers={"powervaro-ci": 0}
    )

    assert isinstance(decision, AdmitDecision)
    assert decision.host == "powervaro-ci"


def test_an_offer_that_shrinks_below_what_is_reserved_defers_new_work(write_config) -> None:
    """The balloon never squeezes running lanes, but the offer can fall under what they
    hold. Admission must then stop, not admit against a negative headroom."""
    config = ControllerConfig.load(write_config(ELASTIC_HOST_CONFIG))
    ledger = Ledger()
    ledger.add(
        Reservation(
            "p1", 3, "alvaro-francisco-gil/homelab", "light", 700, False, host="powervaro-ci"
        )
    )

    grown, shrunk = (
        evaluate(
            [_homelab_job(2)], ledger, config, healthy=DESKTOP_ONLY, ram_offers={"powervaro-ci": mb}
        )[0]
        for mb in (1400, 600)
    )

    assert isinstance(grown, AdmitDecision)
    assert isinstance(shrunk, DeferDecision)
    assert BUDGET_FULL in shrunk.reasons
