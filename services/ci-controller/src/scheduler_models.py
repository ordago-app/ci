from __future__ import annotations

from dataclasses import asdict
from typing import Any

from pydantic import BaseModel, Field

from src.holds import HOLD_TTL_MAX_SECONDS, HOLD_TTL_MIN_SECONDS, Hold
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


class HoldRequest(BaseModel):
    """No default host, ever (invariant 4): a hold names the machine it protects."""

    host: str
    ram_mb: int = Field(gt=0)
    ttl_seconds: int = Field(ge=HOLD_TTL_MIN_SECONDS, le=HOLD_TTL_MAX_SECONDS)


class HoldWire(BaseModel):
    name: str
    host: str
    ram_mb: int
    expires_at: float
    remaining_seconds: int


class HostRamWire(BaseModel):
    budget_ram_mb: int
    lanes_ram_mb: int
    held_ram_mb: int


class HoldList(BaseModel):
    holds: list[HoldWire]
    hosts: dict[str, HostRamWire]


def hold_to_wire(hold: Hold, now: float) -> HoldWire:
    return HoldWire(
        name=hold.name,
        host=hold.host,
        ram_mb=hold.ram_mb,
        expires_at=hold.expires_at,
        remaining_seconds=hold.remaining_seconds(now),
    )


def hold_from_wire(data: dict[str, Any]) -> Hold:
    return Hold(
        name=data["name"], host=data["host"], ram_mb=data["ram_mb"], expires_at=data["expires_at"]
    )
