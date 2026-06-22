from __future__ import annotations

from src.config import ControllerConfig
from src.host_stats import HostStats
from src.ledger import Ledger
from src.models import (
    ALREADY_RUNNING,
    BUDGET_FULL,
    DISK_FULL,
    HOST_PRESSURE,
    KVM_BUSY,
    LANE_CEILING,
    NOT_ALLOWLISTED,
    AdmitDecision,
    Decision,
    DeferDecision,
    QueuedJob,
)


def evaluate(
    jobs: list[QueuedJob],
    ledger: Ledger,
    config: ControllerConfig,
    host_stats: HostStats | None = None,
) -> list[Decision]:
    """Decide admit/defer per job, accumulating admissions within the batch."""
    ram = ledger.total_ram()
    lanes = ledger.lane_count()
    kvm = ledger.kvm_in_use()
    # Per-disk GB committed so far (ledger + this batch's admissions).
    disk_gb = {disk: ledger.disk_gb_in_use(disk) for disk in config.disk_budget_gb}
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
        disk_budget = config.disk_budget_gb.get(job_class.work_disk)
        if (
            disk_budget is not None
            and disk_gb[job_class.work_disk] + job_class.work_gb > disk_budget
        ):
            decisions.append(DeferDecision(job, DISK_FULL))
            continue

        if (
            config.admission_mode == "reservation_plus_guard"
            and host_stats is not None
            and (
                host_stats.mem_available_mb < config.host_free_ram_floor_mb
                or (config.host_load_ceiling > 0 and host_stats.load_1m > config.host_load_ceiling)
            )
        ):
            decisions.append(DeferDecision(job, HOST_PRESSURE))
            continue

        decisions.append(
            AdmitDecision(
                job=job,
                class_name=class_name,
                ram_mb=job_class.ram_mb,
                needs_kvm=job_class.needs_kvm,
                work_disk=job_class.work_disk,
                work_gb=job_class.work_gb,
            )
        )
        ram += job_class.ram_mb
        lanes += 1
        kvm = kvm or job_class.needs_kvm
        if job_class.work_disk in disk_gb:
            disk_gb[job_class.work_disk] += job_class.work_gb

    return decisions
