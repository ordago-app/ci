from __future__ import annotations

import asyncio

from fastapi import FastAPI
from pydantic import BaseModel

from .worker import ReviewWorker


class ReviewRequest(BaseModel):
    repo: str
    pr_number: int


def create_app(worker: ReviewWorker) -> FastAPI:
    app = FastAPI(title="github-review")
    # Serialize job execution (codex containers + SQLite writes) across the
    # poll task and HTTP triggers.
    lock = asyncio.Lock()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/reviews")
    async def reviews(req: ReviewRequest) -> dict[str, object]:
        async with lock:
            summary = await asyncio.to_thread(worker.run_pr_review, req.repo, req.pr_number)
        return {
            "head_sha": summary.head_sha,
            "verdict": summary.verdict,
            "escalated": summary.escalated,
        }

    return app
