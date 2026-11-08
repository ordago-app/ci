from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from .job_store import JobStatus, ReviewJob
from .worker import BUDGET_SKIP_PREFIX, ReviewWorker

_log = logging.getLogger("github_review.poll")


class ReviewRequest(BaseModel):
    repo: str
    pr_number: int


async def _safe_tick(worker: ReviewWorker) -> None:
    # One failing tick (transient GitHub error, one bad job) must never kill the
    # poll loop — otherwise polling silently stops until the container restarts.
    try:
        await asyncio.to_thread(worker.tick)
    except Exception:
        _log.exception("poll tick failed; continuing")


def _job_state(job: ReviewJob, worker: ReviewWorker) -> dict[str, object]:
    """One head's review, as a caller waiting on it needs to read it.

    `done` is the only field a waiter must branch on: true once nothing more
    will happen to this head without a new request."""
    exhausted = job.status == JobStatus.FAILED and job.attempts >= worker.max_attempts
    return {
        "repo": job.repo,
        "pr_number": job.pr_number,
        "head_sha": job.head_sha,
        "status": str(job.status),
        "done": job.status in (JobStatus.POSTED, JobStatus.SKIPPED) or exhausted,
        "verdict": job.verdict,
        "budget_exhausted": job.status == JobStatus.SKIPPED
        and (job.last_error or "").startswith(BUDGET_SKIP_PREFIX),
        "reviewer": worker.reviewer_bot,
        "provider": job.provider,
        "attempts": job.attempts,
        "last_error": job.last_error,
    }


def _head_state(job: ReviewJob, worker: ReviewWorker) -> str:
    """pending | running | approved | changes_requested | failed | skipped.

    A failure with attempts left is `pending`: the worker will retry it. A posted
    row with no verdict predates verdict recording and reads as REQUEST_CHANGES,
    as it does everywhere else."""
    if job.status == JobStatus.QUEUED:
        return "pending"
    if job.status == JobStatus.RUNNING:
        return "running"
    if job.status == JobStatus.POSTED:
        return "approved" if job.verdict == "APPROVE" else "changes_requested"
    if job.status == JobStatus.FAILED:
        return "failed" if job.attempts >= worker.max_attempts else "pending"
    return "skipped"


# How recently a review must have failed to still count as "stuck". A terminal failure
# is never cleared by anything in this service, so without a bound the count only grows
# and the badge it feeds is permanently lit. Seven days is long enough that a failure
# over a weekend is still showing on Monday, and short enough that the number returns to
# zero on its own once you have dealt with it — or once it has stopped mattering.
STUCK_WINDOW_SECONDS = 7 * 24 * 3600

# The poll loop counts as hung after this many intervals with no finished tick.
# A tick only polls GitHub and queues heads, so it is short; several intervals of
# silence means the loop itself is stuck, while the HTTP side still answers.
POLL_STALE_INTERVALS = 5


def create_app(
    worker: ReviewWorker,
    poll_interval: int = 0,
    stuck_window_seconds: float = STUCK_WINDOW_SECONDS,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    # Set by every enqueue, so a requested review starts now rather than on the
    # next scheduled tick. A set that lands mid-tick is not lost: the wait after
    # that tick returns at once.
    wake = asyncio.Event()
    # When the last tick finished, failed ticks included: a tick that raised
    # still proves the loop is alive. Seeded with startup for its grace period.
    last_tick_done = [clock()]

    async def _poll_loop() -> None:
        _log.info("poll loop started (interval=%ss)", poll_interval)
        while True:
            await _safe_tick(worker)
            last_tick_done[0] = clock()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=poll_interval)
            wake.clear()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = None
        if poll_interval > 0:
            worker.start()
            task = asyncio.create_task(_poll_loop())
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
        if poll_interval > 0:
            silent_for = clock() - last_tick_done[0]
            if silent_for > POLL_STALE_INTERVALS * poll_interval:
                raise HTTPException(
                    status_code=503, detail=f"poll loop silent for {int(silent_for)}s"
                )
        return {"status": "ok"}

    @app.get("/status")
    async def status() -> dict[str, object]:
        """Queue state for the dashboard. Read-only; takes no lock, so a long
        in-flight review never blocks the glance page."""
        store = worker.store
        return {
            "counts": store.counts_by_status(),
            # `counts` are lifetime totals, so counts["failed"] only ever grows and says
            # nothing about now. This is the live figure: failed, out of retries, AND
            # failed inside the window. The dashboard's attention badge reads this, and
            # the window is what lets it fall back to zero — nothing here ever clears a
            # terminal failure, so without it this number is monotonic too.
            "stuck": store.count_stuck(worker.max_attempts, stuck_window_seconds, time.time()),
            # Providers currently believed down, newest outage first. The pull
            # side of the alert: the dashboard polls /status every ~30s, so a
            # spent subscription shows up without waiting for someone to open a
            # PR and read a review banner.
            "degraded_providers": worker.degraded_providers(),
            # Per-account remaining quota, in chain order. Additive: an older
            # dashboard that predates the field renders one card fewer, not a
            # broken page.
            "reviewer_quota": worker.reviewer_quota(),
            # Reported run cost per account, over the last day and 30 days. An
            # api_key account spends somebody's credit, and this is where the
            # owner reads how much.
            "spend": {
                "day": store.spend_by_account(time.time() - 86400),
                "month": store.spend_by_account(time.time() - 30 * 86400),
            },
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

    @app.post("/reviews", status_code=202)
    async def request_review(req: ReviewRequest) -> dict[str, object]:
        """Queue a review of the PR's current head and return at once."""
        try:
            job = await asyncio.to_thread(worker.request_review, req.repo, req.pr_number)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        wake.set()
        return _job_state(job, worker)

    @app.get("/reviews/{owner}/{name}/{pr_number}/{head_sha}")
    async def review_state(
        owner: str, name: str, pr_number: int, head_sha: str
    ) -> dict[str, object]:
        """Read-only, from the store: polling it costs no GitHub call."""
        job = worker.store.find(f"{owner}/{name}", pr_number, head_sha)
        if job is None:
            raise HTTPException(status_code=404, detail="no review requested for that head")
        return _job_state(job, worker)

    @app.get("/heads")
    async def head_states(
        repo: str, sha: Annotated[list[str], Query(min_length=1)]
    ) -> dict[str, object]:
        """Review state of each asked-for head, from the store: no GitHub call.

        ci-controller orders CI admission by it. A head with no row is left out
        rather than guessed at: the caller decides what "never reviewed" means."""
        jobs = worker.store.latest_for_heads(repo, sha)
        return {
            "repo": repo,
            "heads": {head: _head_state(job, worker) for head, job in jobs.items()},
        }

    return app
