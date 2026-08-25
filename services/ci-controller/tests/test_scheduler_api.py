from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from src.config import ControllerConfig
from src.ledger import Ledger
from src.scheduler import LocalScheduler
from src.scheduler_api import create_scheduler_app

from tests.conftest import VALID_CONFIG


@pytest.fixture()
def client(write_config):
    config = ControllerConfig.load(write_config(VALID_CONFIG))
    return TestClient(create_scheduler_app(LocalScheduler(config=config, ledger=Ledger())))


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_plan_returns_an_admit_decision(client):
    body = {
        "jobs": [{"job_id": 1, "repo": "ordago-app/ordago-apps", "labels": ["ordago-ci"]}],
        "host_stats": {},
        "healthy": None,
    }
    decisions = client.post("/plan", json=body).json()["decisions"]

    assert len(decisions) == 1
    assert decisions[0]["kind"] == "admit"
    assert decisions[0]["class_name"] == "light"


def test_commit_update_and_release(client):
    body = {
        "jobs": [{"job_id": 1, "repo": "ordago-app/ordago-apps", "labels": ["ordago-ci"]}],
        "host_stats": {},
        "healthy": None,
    }
    decision = client.post("/plan", json=body).json()["decisions"][0]

    r = client.post(
        "/lanes",
        json={"decision": decision, "lane_id": "lane-a", "container_id": "c1", "idle_since": 1.0},
    )
    assert r.status_code == 204

    assert client.patch("/lanes/lane-a", json={"fields": {"running_job_id": 42}}).status_code == 204
    assert client.get("/lanes").json()["lanes"][0]["running_job_id"] == 42

    assert client.delete("/lanes/lane-a").status_code == 204
    assert client.get("/lanes").json()["lanes"] == []


def test_update_unknown_lane_is_404(client):
    r = client.patch("/lanes/nope", json={"fields": {"running_job_id": 1}})
    assert r.status_code == 404


def test_adopt_reinserts_a_reservation(client):
    res = {
        "lane_id": "lane-z",
        "spawned_for_job_id": 5,
        "repo": "o/r",
        "class_name": "light",
        "ram_mb": 700,
        "needs_kvm": False,
        "work_disk": "ssd",
        "work_gb": 0,
        "workflow": "",
        "job_name": "",
        "host": "powerserver",
        "started_at": None,
        "running_job_id": None,
        "idle_since": None,
        "runner_id": None,
        "container_id": "c9",
        "reap_blocked_reason": None,
        "reap_block_count": 0,
    }
    assert client.post("/lanes/adopt", json={"reservation": res}).status_code == 204
    assert client.get("/lanes").json()["lanes"][0]["lane_id"] == "lane-z"
