from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from src.controller import Controller

log = logging.getLogger("ci-controller")

# 12 ticks -- a minute at the deployed POLL_INTERVAL_SECONDS of 5. Long enough that a
# scheduler restart or a GitHub blip rides through untouched, short enough that a
# stranded dispatcher is back within a minute rather than however long it takes a
# human to notice CI is quiet. Both silent stalls so far were found by a person
# wondering why no job had started.
DEFAULT_MAX_CONSECUTIVE_TICK_FAILURES = 12


@dataclass
class _TickHealth:
    """Shared between the tick loop and /healthz."""

    consecutive_failures: int = 0
    last_error: str = ""


def _terminate() -> None:
    """SIGTERM rather than os._exit: uvicorn then unwinds the lifespan, so the tick
    task is cancelled and in-flight requests finish. `restart: unless-stopped` restarts
    the container regardless of exit status, and the fresh container re-enters
    ci-fabric's CURRENT network namespace -- which is the whole point."""
    signal.raise_signal(signal.SIGTERM)


def create_app(
    controller: Controller,
    poll_interval: float,
    *,
    max_consecutive_tick_failures: int = DEFAULT_MAX_CONSECUTIVE_TICK_FAILURES,
    on_fatal: Callable[[], None] | None = None,
) -> FastAPI:
    fatal = on_fatal if on_fatal is not None else _terminate
    health = _TickHealth()

    async def _loop() -> None:
        while True:
            try:
                await asyncio.to_thread(controller.tick)
            except Exception as exc:
                health.consecutive_failures += 1
                health.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("tick failed (%d consecutive): %s", health.consecutive_failures, exc)
                if health.consecutive_failures >= max_consecutive_tick_failures:
                    log.critical(
                        "%d consecutive tick failures (last: %s) -- exiting so the "
                        "restart policy can recover; a dispatcher that cannot reach "
                        "the scheduler dispatches nothing and must not look healthy",
                        health.consecutive_failures,
                        health.last_error,
                    )
                    fatal()
                    return
            else:
                health.consecutive_failures = 0
                health.last_error = ""
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
    async def healthz() -> dict[str, object]:
        # Deliberately still 200 while degraded: the dashboard polls this, and the
        # process exits on its own once the failures are sustained. The count is here
        # to be *seen* -- a flat "ok" is what hid both silent stalls.
        body: dict[str, object] = {
            "status": "ok" if health.consecutive_failures == 0 else "degraded",
            "consecutive_tick_failures": health.consecutive_failures,
        }
        if health.last_error:
            body["last_tick_error"] = health.last_error
        return body

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
