from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from src.controller import Controller

log = logging.getLogger("ci-controller")


def create_app(controller: Controller, poll_interval: float) -> FastAPI:
    async def _loop() -> None:
        while True:
            try:
                await asyncio.to_thread(controller.tick)
            except Exception as exc:
                log.exception("tick failed: %s", exc)
            await asyncio.sleep(poll_interval)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_loop()) if poll_interval > 0 else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="ci-controller", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status")
    async def status() -> dict:
        return controller.status()

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> str:
        s = controller.status()
        lines = [
            f"ci_budget_ram_mb {s['budget_ram_mb']}",
            f"ci_ledger_ram_mb {s['ledger_ram_mb']}",
            f"ci_lanes_running {s['lanes_running']}",
            f"ci_max_lanes {s['max_lanes']}",
            f"ci_kvm_in_use {1 if s['kvm_in_use'] else 0}",
            f"ci_jobs_deferred {len(s['deferred'])}",
        ]
        return "\n".join(lines) + "\n"

    return app
