from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .worker import ReviewWorker

_log = logging.getLogger("github_review.poll")


class ReviewRequest(BaseModel):
    repo: str
    pr_number: int


async def _safe_tick(worker: ReviewWorker, lock: asyncio.Lock) -> None:
    # One failing tick (transient GitHub error, one bad job) must never kill the
    # poll loop — otherwise polling silently stops until the container restarts.
    try:
        async with lock:
            await asyncio.to_thread(worker.tick)
    except Exception:
        _log.exception("poll tick failed; continuing")


def create_app(worker: ReviewWorker, poll_interval: int = 0) -> FastAPI:
    lock = asyncio.Lock()

    async def _poll_loop() -> None:
        _log.info("poll loop started (interval=%ss)", poll_interval)
        while True:
            await _safe_tick(worker, lock)
            await asyncio.sleep(poll_interval)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_poll_loop()) if poll_interval > 0 else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="github-review", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status")
    async def status() -> dict[str, object]:
        """Queue state for the dashboard. Read-only; takes no lock, so a long
        in-flight review never blocks the glance page."""
        store = worker.store
        return {
            "counts": store.counts_by_status(),
            # `counts` are lifetime totals, so counts["failed"] only ever grows and says
            # nothing about now. This is the live figure: failed AND out of retries, i.e.
            # what actually needs a human. The dashboard's attention badge reads this.
            "stuck": store.count_stuck(worker.max_attempts),
            "active": [
                {
                    "id": job.id,
                    "repo": job.repo,
                    "pr_number": job.pr_number,
                    "status": str(job.status),
                    "attempts": job.attempts,
                    "queued_at": job.queued_at,
                    "last_error": job.last_error,
                }
                for job in store.list_active()
            ],
        }

    @app.post("/reviews")
    async def reviews(req: ReviewRequest) -> dict[str, object]:
        async with lock:
            try:
                summary = await asyncio.to_thread(worker.run_pr_review, req.repo, req.pr_number)
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "head_sha": summary.head_sha,
            "verdict": summary.verdict,
            "escalated": summary.escalated,
            "reviewer": worker.reviewer_bot,
        }

    return app
