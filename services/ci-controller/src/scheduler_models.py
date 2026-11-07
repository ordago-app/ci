from __future__ import annotations

from dataclasses import asdict
from typing import Any

from pydantic import BaseModel

from src.models import AdmitDecision, Decision, DeferDecision, QueuedJob, Reservation


def decision_to_wire(decision: Decision) -> dict[str, Any]:
    if isinstance(decision, AdmitDecision):
        return {"kind": "admit", **asdict(decision)}
    return {"kind": "defer", **asdict(decision), "reasons": list(decision.reasons)}


def decision_from_wire(data: dict[str, Any]) -> Decision:
    payload = dict(data)
    kind = payload.pop("kind")
    job = QueuedJob(**payload.pop("job"))
    if kind == "admit":
        return AdmitDecision(job=job, **payload)
    if kind == "defer":
        gates_by_host = {
            host: tuple(gates) for host, gates in payload.pop("gates_by_host", {}).items()
        }
        return DeferDecision(
            job, tuple(payload.pop("reasons")), gates_by_host=gates_by_host, **payload
        )
    raise ValueError(f"unknown decision kind: {kind!r}")


def reservation_to_wire(res: Reservation) -> dict[str, Any]:
    return asdict(res)


def reservation_from_wire(data: dict[str, Any]) -> Reservation:
    return Reservation(**data)


class HostStatsWire(BaseModel):
    mem_available_mb: int
    load_1m: float
    # Optional on the wire so a dispatcher and scheduler from either side of this
    # field's introduction still talk during a rolling redeploy.
    cpu_pressure_pct: float | None = None


class PlanRequest(BaseModel):
    jobs: list[dict[str, Any]]
    host_stats: dict[str, HostStatsWire]
    healthy: list[str] | None
    review_sessions: int
    # Defaulted so a dispatcher from before elastic hosts still talks to this scheduler
    # mid-redeploy. The default is the safe side: an elastic host with no offer admits
    # nothing (admission.ram_budget_mb), it never falls back to its ceiling.
    ram_offers: dict[str, int] = {}


class PlanResponse(BaseModel):
    decisions: list[dict[str, Any]]


class CommitRequest(BaseModel):
    decision: dict[str, Any]
    lane_id: str
    container_id: str
    idle_since: float


class UpdateRequest(BaseModel):
    fields: dict[str, Any]


class AdoptRequest(BaseModel):
    reservation: dict[str, Any]


class LaneList(BaseModel):
    lanes: list[dict[str, Any]]
