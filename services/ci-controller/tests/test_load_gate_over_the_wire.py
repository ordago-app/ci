"""The CPU-pressure reading must survive the dispatcher -> scheduler hop.

Admission runs in the scheduler, which never reads /proc itself: the dispatcher
sends HostStats over POST /plan. A field dropped on either side of the wire would
silently fall back to the plain load-average gate and bring back the 2026-10-20
starvation (docs/incidents/2026-10-20-ci-held-by-io-wait-load.md) while every
direct `evaluate()` test still passed. So these go through the real client and
the real app.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from src.config import ControllerConfig
from src.host_stats import HostStats
from src.ledger import Ledger
from src.models import AdmitDecision, DeferDecision, QueuedJob
from src.scheduler import LocalScheduler
from src.scheduler_api import create_scheduler_app
from src.scheduler_client import HttpScheduler

from tests.conftest import VALID_CONFIG

GUARDED = VALID_CONFIG.replace(
    "ram_budget_mb: 12000",
    "admission_mode: reservation_plus_guard\nhost_load_ceiling: 16.0\nram_budget_mb: 12000",
)
JOB = QueuedJob(job_id=1, repo="ordago-app/ordago-apps", labels=["ordago-ci"])


@pytest.fixture()
def scheduler(write_config) -> HttpScheduler:
    config = ControllerConfig.load(write_config(GUARDED))
    app = TestClient(create_scheduler_app(LocalScheduler(config=config, ledger=Ledger())))

    def forward(request: httpx.Request) -> httpx.Response:
        resp = app.request(
            request.method, request.url.path, content=request.content, headers=request.headers
        )
        return httpx.Response(resp.status_code, content=resp.content, headers=resp.headers)

    return HttpScheduler(
        "http://sched:8001", transport=httpx.MockTransport(forward), retries=1, backoff=0.0
    )


def _plan(scheduler: HttpScheduler, stats: HostStats):
    return scheduler.plan([JOB], {"powerserver": stats}, None)[0]


def test_io_wait_load_admits_through_the_wire(scheduler) -> None:
    stats = HostStats(mem_available_mb=9000, load_1m=40.0, cpu_pressure_pct=0.2)
    assert isinstance(_plan(scheduler, stats), AdmitDecision)


def test_cpu_contended_load_defers_through_the_wire(scheduler) -> None:
    stats = HostStats(mem_available_mb=9000, load_1m=40.0, cpu_pressure_pct=95.0)
    decision = _plan(scheduler, stats)
    assert isinstance(decision, DeferDecision)
    assert decision.reason == "host_pressure"


def test_a_plan_without_the_field_keeps_the_load_only_gate(write_config) -> None:
    # A dispatcher from before the field: the scheduler must not read its absence
    # as "CPUs idle".
    config = ControllerConfig.load(write_config(GUARDED))
    app = TestClient(create_scheduler_app(LocalScheduler(config=config, ledger=Ledger())))
    body = {
        "jobs": [{"job_id": 1, "repo": "ordago-app/ordago-apps", "labels": ["ordago-ci"]}],
        "host_stats": {"powerserver": {"mem_available_mb": 9000, "load_1m": 40.0}},
        "healthy": None,
    }
    decision = app.post("/plan", json=body).json()["decisions"][0]
    assert decision["kind"] == "defer"
    assert "host_pressure" in decision["reasons"]
