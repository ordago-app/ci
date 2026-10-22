import time
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
    assert resp.json()["status"] == "ok"


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
        "config_version": "test-ver",
        "admission_mode": "reservation",
        "disk_gb": {},
    }
    client = _client(payload)
    body = client.get("/metrics").text
    assert "ci_budget_ram_mb 12000" in body
    assert "ci_ledger_ram_mb 700" in body
    assert "ci_lanes_running 1" in body
    assert "ci_kvm_in_use 0" in body


def test_metrics_exposes_ci_lanes_booting_gauge() -> None:
    """status() only ever produces busy/booting — a lane 20 s into its boot is not idle
    capacity, so the gauge, the /status field and the CLI label all say booting."""
    payload = {
        "budget_ram_mb": 9000,
        "ledger_ram_mb": 1400,
        "lanes_running": 2,
        "max_lanes": 4,
        "kvm_in_use": False,
        "running": [
            {"lane_id": "powerserver-cici-7", "job_id": None, "state": "booting"},
            {"lane_id": "powerserver-cici-8", "job_id": 901, "state": "busy"},
        ],
        "deferred": [],
        "config_version": "abc123",
        "admission_mode": "reservation",
        "disk_gb": {},
    }
    client = _client(payload)
    body = client.get("/metrics").text
    assert "ci_lanes_booting 1" in body
    assert "ci_lanes_idle" not in body


def test_metrics_exposes_info_and_disk_gauges() -> None:
    payload = {
        "budget_ram_mb": 9000,
        "ledger_ram_mb": 0,
        "lanes_running": 0,
        "max_lanes": 4,
        "kvm_in_use": False,
        "running": [],
        "deferred": [],
        "config_version": "abc123",
        "admission_mode": "reservation",
        "disk_gb": {
            "ssd": {"used": 20, "budget": 500},
            "hdd": {"used": 5, "budget": 1000},
        },
    }
    client = _client(payload)
    body = client.get("/metrics").text
    assert "ci_controller_info{" in body
    assert 'config_version="abc123"' in body
    assert 'admission_mode="reservation"' in body
    assert 'ci_disk_used_gb{disk="ssd"}' in body
    assert 'ci_disk_budget_gb{disk="hdd"}' in body


def test_healthz_reports_consecutive_tick_failures() -> None:
    """/healthz returning a flat `ok` is how both silent-stall incidents stayed
    invisible: on 2026-08-26 MagicDNS took every poll down, and on 2026-08-31 a
    ci-fabric restart stranded the dispatcher in a dead network namespace. In both
    cases every container was Up and /healthz said ok while nothing was dispatched.
    The tick failure count is the one number that would have shown it."""
    client = _client({})
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["consecutive_tick_failures"] == 0


def test_loop_terminates_the_process_after_sustained_tick_failure() -> None:
    """The dispatcher shares ci-fabric's network namespace (network_mode:
    service:ci-fabric). When the sidecar restarts, the namespace the dispatcher holds
    is dead and never recovers -- every tick raises SchedulerUnavailable forever while
    the container reports Up and restart=0. Exiting hands recovery to the restart
    policy, which rejoins the live namespace."""
    controller = MagicMock()
    controller.tick.side_effect = RuntimeError("scheduler unreachable")
    fatal = MagicMock()

    app = create_app(
        controller,
        poll_interval=0.001,
        max_consecutive_tick_failures=3,
        on_fatal=fatal,
    )
    with TestClient(app):
        deadline = time.monotonic() + 5
        while not fatal.called and time.monotonic() < deadline:
            time.sleep(0.01)

    assert fatal.called, "sustained tick failure must terminate the process"
    assert controller.tick.call_count >= 3


def test_loop_does_not_terminate_when_ticks_recover() -> None:
    """A transient failure is not a stranding. Only an unbroken run of failures is."""
    controller = MagicMock()
    controller.tick.side_effect = [RuntimeError("blip"), None, RuntimeError("blip"), None] + [
        None
    ] * 50
    fatal = MagicMock()

    app = create_app(
        controller,
        poll_interval=0.001,
        max_consecutive_tick_failures=3,
        on_fatal=fatal,
    )
    with TestClient(app):
        deadline = time.monotonic() + 1
        while controller.tick.call_count < 10 and time.monotonic() < deadline:
            time.sleep(0.01)

    assert not fatal.called, "an interleaved failure must not trip the exit threshold"
