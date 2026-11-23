from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from src.admission import evaluate, ram_ceiling_mb
from src.config import ControllerConfig
from src.holds import Hold, HoldBook
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
        *,
        review_sessions: int,
        ram_offers: dict[str, int],
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

    def holds(self) -> list[Hold]: ...


class LocalScheduler:
    """In-process Scheduler. Behaviour-identical to the pre-split controller.

    Owns the hold book too (src/holds.py): in memory like the ledger, so a restart drops
    every hold and their owners renew them."""

    def __init__(
        self,
        config: ControllerConfig,
        ledger: Ledger,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self._holds = HoldBook(clock)

    def plan(
        self,
        jobs: list[QueuedJob],
        host_stats: dict[str, HostStats],
        healthy: set[str] | None,
        *,
        review_sessions: int,
        ram_offers: dict[str, int],
    ) -> list[Decision]:
        return evaluate(
            jobs,
            self.ledger,
            self.config,
            host_stats,
            healthy,
            review_sessions=review_sessions,
            ram_offers=ram_offers,
            held_ram_mb=self._holds.held_ram_mb(),
        )

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

    def holds(self) -> list[Hold]:
        return self._holds.active()

    def now(self) -> float:
        return self._holds.now()

    def put_hold(self, name: str, *, host: str, ram_mb: int, ttl_seconds: int) -> Hold:
        """Raises ValueError for a host that is not configured -- a hold never lands on a
        default machine (invariant 4) -- or one larger than the host's whole RAM ceiling,
        which could never fit and would wait out its TTL holding every lane off."""
        hosts = self.config.resolved_hosts()
        if host not in hosts:
            raise ValueError(f"unknown host {host!r}; configured: {', '.join(sorted(hosts))}")
        ceiling = ram_ceiling_mb(hosts[host], self.config)
        if ram_mb > ceiling:
            raise ValueError(f"{ram_mb} MB exceeds {host}'s whole {ceiling} MB budget")
        return self._holds.put(name, host=host, ram_mb=ram_mb, ttl_seconds=ttl_seconds)

    def release_hold(self, name: str) -> None:
        self._holds.remove(name)

    def host_ram(self) -> dict[str, dict[str, int]]:
        """Per host: its configured RAM ceiling (no elastic offer, which only a dispatcher
        reads), what its lanes reserve, and what holds set aside on it."""
        held = self._holds.held_ram_mb()
        return {
            name: {
                "budget_ram_mb": ram_ceiling_mb(host, self.config),
                "lanes_ram_mb": self.ledger.total_ram(name),
                "held_ram_mb": held.get(name, 0),
            }
            for name, host in self.config.resolved_hosts().items()
        }
