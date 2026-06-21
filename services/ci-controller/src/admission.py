from __future__ import annotations

from src.config import ControllerConfig
from src.ledger import Ledger
from src.models import (
    ALREADY_RUNNING,
    BUDGET_FULL,
    KVM_BUSY,
    LANE_CEILING,
    NOT_ALLOWLISTED,
    AdmitDecision,
    Decision,
    DeferDecision,
    QueuedJob,
)


def evaluate(jobs: list[QueuedJob], ledger: Ledger, config: ControllerConfig) -> list[Decision]:
    """Decide admit/defer per job, accumulating admissions within the batch."""
    ram = ledger.total_ram()
    lanes = ledger.lane_count()
    kvm = ledger.kvm_in_use()
    decisions: list[Decision] = []

    for job in jobs:
        if ledger.has_job(job.job_id):
            decisions.append(DeferDecision(job, ALREADY_RUNNING))
            continue

        class_name = config.class_for(job.repo, job.labels)
        if class_name is None:
            decisions.append(DeferDecision(job, NOT_ALLOWLISTED))
            continue

        job_class = config.classes[class_name]

        if lanes >= config.max_concurrent_lanes:
            decisions.append(DeferDecision(job, LANE_CEILING))
            continue
        if job_class.needs_kvm and kvm:
            decisions.append(DeferDecision(job, KVM_BUSY))
            continue
        if ram + job_class.ram_mb > config.ram_budget_mb:
            decisions.append(DeferDecision(job, BUDGET_FULL))
            continue

        decisions.append(
            AdmitDecision(
                job=job,
                class_name=class_name,
                ram_mb=job_class.ram_mb,
                needs_kvm=job_class.needs_kvm,
            )
        )
        ram += job_class.ram_mb
        lanes += 1
        kvm = kvm or job_class.needs_kvm

    return decisions
