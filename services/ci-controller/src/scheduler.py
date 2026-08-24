from __future__ import annotations

from typing import Protocol

from src.admission import evaluate
from src.config import ControllerConfig
from src.host_stats import HostStats
from src.ledger import Ledger
from src.models import AdmitDecision, Decision, QueuedJob, Reservation


class Scheduler(Protocol):
    """Capacity authority: owns the reservation ledger and every admission verdict.

    Deliberately knows nothing about GitHub or Docker. That is the whole point of the
    boundary — a scheduler serving two organisations must not be able to authenticate
    as either (see docs/plans/ideas/federated-ci-pool.md, decision 1)."""

    def plan(
        self,
        jobs: list[QueuedJob],
        host_stats: dict[str, HostStats],
        healthy: set[str] | None,
    ) -> list[Decision]: ...

    def commit(
        self,
        decision: AdmitDecision,
        *,
        lane_id: str,
        container_id: str,
        idle_since: float,
    ) -> None: ...

    def update(self, lane_id: str, **fields: object) -> None: ...

    def release(self, lane_id: str) -> None: ...

    def adopt(self, reservation: Reservation) -> None: ...

    def lanes(self) -> list[Reservation]: ...


class LocalScheduler:
    """In-process Scheduler. Behaviour-identical to the pre-split controller."""

    def __init__(self, config: ControllerConfig, ledger: Ledger) -> None:
        self.config = config
        self.ledger = ledger

    def plan(
        self,
        jobs: list[QueuedJob],
        host_stats: dict[str, HostStats],
        healthy: set[str] | None,
    ) -> list[Decision]:
        return evaluate(jobs, self.ledger, self.config, host_stats, healthy)

    def commit(
        self,
        decision: AdmitDecision,
        *,
        lane_id: str,
        container_id: str,
        idle_since: float,
    ) -> None:
        self.ledger.add(
            Reservation(
                lane_id=lane_id,
                spawned_for_job_id=decision.job.job_id,
                repo=decision.job.repo,
                class_name=decision.class_name,
                ram_mb=decision.ram_mb,
                needs_kvm=decision.needs_kvm,
                work_disk=decision.work_disk,
                work_gb=decision.work_gb,
                workflow=decision.job.workflow,
                job_name=decision.job.job_name,
                host=decision.host,
                idle_since=idle_since,
                container_id=container_id,
            )
        )

    def update(self, lane_id: str, **fields: object) -> None:
        self.ledger.update(lane_id, **fields)

    def release(self, lane_id: str) -> None:
        self.ledger.remove(lane_id)

    def adopt(self, reservation: Reservation) -> None:
        """Re-insert a lane the controller found running but the ledger had lost —
        a controller restart, or a lane that outlived its reservation."""
        self.ledger.add(reservation)

    def lanes(self) -> list[Reservation]:
        return self.ledger.reservations()
