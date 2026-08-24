from __future__ import annotations

from fastapi import FastAPI, HTTPException, Response

from src.host_stats import HostStats
from src.models import AdmitDecision, QueuedJob
from src.scheduler import LocalScheduler
from src.scheduler_models import (
    AdoptRequest,
    CommitRequest,
    LaneList,
    PlanRequest,
    PlanResponse,
    UpdateRequest,
    decision_from_wire,
    decision_to_wire,
    reservation_from_wire,
    reservation_to_wire,
)


def create_scheduler_app(scheduler: LocalScheduler) -> FastAPI:
    app = FastAPI(title="ci-scheduler")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/plan", response_model=PlanResponse)
    async def plan(req: PlanRequest) -> PlanResponse:
        jobs = [QueuedJob(**j) for j in req.jobs]
        stats = {name: HostStats(**s.model_dump()) for name, s in req.host_stats.items()}
        healthy = set(req.healthy) if req.healthy is not None else None
        decisions = scheduler.plan(jobs, stats, healthy)
        return PlanResponse(decisions=[decision_to_wire(d) for d in decisions])

    @app.post("/lanes", status_code=204)
    async def commit(req: CommitRequest) -> Response:
        decision = decision_from_wire(req.decision)
        if not isinstance(decision, AdmitDecision):
            raise HTTPException(400, "only an admit decision can be committed")
        scheduler.commit(
            decision,
            lane_id=req.lane_id,
            container_id=req.container_id,
            idle_since=req.idle_since,
        )
        return Response(status_code=204)

    @app.post("/lanes/adopt", status_code=204)
    async def adopt(req: AdoptRequest) -> Response:
        scheduler.adopt(reservation_from_wire(req.reservation))
        return Response(status_code=204)

    @app.patch("/lanes/{lane_id}", status_code=204)
    async def update(lane_id: str, req: UpdateRequest) -> Response:
        try:
            scheduler.update(lane_id, **req.fields)
        except KeyError:
            raise HTTPException(404, f"unknown lane: {lane_id}") from None
        return Response(status_code=204)

    @app.delete("/lanes/{lane_id}", status_code=204)
    async def release(lane_id: str) -> Response:
        scheduler.release(lane_id)
        return Response(status_code=204)

    @app.get("/lanes", response_model=LaneList)
    async def lanes() -> LaneList:
        return LaneList(lanes=[reservation_to_wire(r) for r in scheduler.lanes()])

    return app
