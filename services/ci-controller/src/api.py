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
        # status() reaches the scheduler over HTTP with blocking retries; called inline it
        # would stall the event loop, and /healthz with it, for the whole retry window.
        return await asyncio.to_thread(controller.status)

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> str:
        s = await asyncio.to_thread(controller.status)
        lines = [
            f"ci_budget_ram_mb {s['budget_ram_mb']}",
            f"ci_ledger_ram_mb {s['ledger_ram_mb']}",
            f"ci_lanes_running {s['lanes_running']}",
            f"ci_lanes_booting {sum(1 for r in s['running'] if r['state'] == 'booting')}",
            f"ci_max_lanes {s['max_lanes']}",
            f"ci_kvm_in_use {1 if s['kvm_in_use'] else 0}",
            f"ci_jobs_deferred {len(s['deferred'])}",
            f'ci_controller_info{{config_version="{s["config_version"]}",'
            f'admission_mode="{s["admission_mode"]}"}} 1',
        ]
        for disk, d in s["disk_gb"].items():
            lines.append(f'ci_disk_used_gb{{disk="{disk}"}} {d["used"]}')
            lines.append(f'ci_disk_budget_gb{{disk="{disk}"}} {d["budget"]}')
        return "\n".join(lines) + "\n"

    return app
