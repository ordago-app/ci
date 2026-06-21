from src.admission import evaluate
from src.config import ControllerConfig
from src.ledger import Ledger
from src.models import (
    BUDGET_FULL,
    KVM_BUSY,
    LANE_CEILING,
    NOT_ALLOWLISTED,
    AdmitDecision,
    DeferDecision,
    QueuedJob,
    Reservation,
)

from tests.conftest import VALID_CONFIG


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
