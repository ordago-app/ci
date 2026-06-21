from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from src.api import create_app


def _client(status_payload):
    controller = MagicMock()
    controller.status.return_value = status_payload
    app = create_app(controller, poll_interval=0)  # 0 => no background loop in tests
    return TestClient(app)


def test_healthz() -> None:
    client = _client({})
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_status_passthrough() -> None:
    payload = {"budget_ram_mb": 12000, "ledger_ram_mb": 700, "lanes_running": 1}
    client = _client(payload)
    assert client.get("/status").json() == payload


def test_metrics_prometheus_text() -> None:
    payload = {
        "budget_ram_mb": 12000,
        "ledger_ram_mb": 700,
        "lanes_running": 1,
        "max_lanes": 8,
        "kvm_in_use": False,
        "running": [],
        "deferred": [{"job_id": 5, "repo": "o/r", "reason": "budget_full"}],
    }
    client = _client(payload)
    body = client.get("/metrics").text
    assert "ci_budget_ram_mb 12000" in body
    assert "ci_ledger_ram_mb 700" in body
    assert "ci_lanes_running 1" in body
    assert "ci_kvm_in_use 0" in body
