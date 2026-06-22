from __future__ import annotations

from dataclasses import dataclass, field

NOT_ALLOWLISTED = "not_allowlisted"
ALREADY_RUNNING = "already_running"
LANE_CEILING = "lane_ceiling"
KVM_BUSY = "kvm_busy"
BUDGET_FULL = "budget_full"
DISK_FULL = "disk_full"
HOST_PRESSURE = "host_pressure"


@dataclass(frozen=True)
class QueuedJob:
    job_id: int
    repo: str
    labels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Reservation:
    lane_id: str
    job_id: int
    repo: str
    class_name: str
    ram_mb: int
    needs_kvm: bool
    work_disk: str = "ssd"
    work_gb: int = 0


@dataclass(frozen=True)
class AdmitDecision:
    job: QueuedJob
    class_name: str
    ram_mb: int
    needs_kvm: bool
    work_disk: str = "ssd"
    work_gb: int = 0


@dataclass(frozen=True)
class DeferDecision:
    job: QueuedJob
    reason: str


Decision = AdmitDecision | DeferDecision
