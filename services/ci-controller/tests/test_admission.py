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
  - repo: alvaro-francisco-gil/homelab
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
  - repo: alvaro-francisco-gil/homelab
    label_class:
      homelab: light
      node: node
"""


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
    decisions = evaluate([job], Ledger(), cfg, low)
    assert isinstance(decisions[0], DeferDecision)
    assert decisions[0].reason == HOST_PRESSURE


def test_guard_admits_when_host_has_headroom(write_config) -> None:
    cfg_text = VALID_CONFIG.replace(
        "ram_budget_mb: 12000", "admission_mode: reservation_plus_guard\nram_budget_mb: 12000"
    )
    cfg = ControllerConfig.load(write_config(cfg_text))
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    ok = HostStats(mem_available_mb=8000, load_1m=1.0)
    assert isinstance(evaluate([job], Ledger(), cfg, ok)[0], AdmitDecision)


def test_reservation_mode_ignores_host_stats(write_config) -> None:
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))  # default reservation
    job = QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    low = HostStats(mem_available_mb=100, load_1m=99.0)
    assert isinstance(evaluate([job], Ledger(), cfg, low)[0], AdmitDecision)


def test_defer_records_every_binding_gate_not_just_the_first(write_config) -> None:
    """lane_ceiling must not mask budget_full: both gates bind, both are recorded."""
    config = ControllerConfig.load(write_config(CROWDED_CONFIG))
    ledger = Ledger()
    ledger.add(
        Reservation(
            lane_id="lane-1",
            job_id=1,
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
