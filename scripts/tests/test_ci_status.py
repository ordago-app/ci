"""ci_status.py is piped into the deployed controller's python, so it runs against
whatever /status that controller serves. These run the real script on a payload."""

import io
import json
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "ci_status.py"

PRESSURE = {
    "mem_available_mb": 11000,
    "load_1m": 27.9,
    "cpu_pressure_pct": 80.0,
    "free_ram_floor_mb": 3500,
    "load_ceiling": 16.0,
    "binding": True,
}


def _payload(pressure: dict | None) -> dict:
    return {
        "hosts": {
            "host-a": {
                "lanes": 1,
                "max_lanes": 5,
                "ram_mb": 5000,
                "budget_ram_mb": 11000,
                "healthy": True,
                "pressure": pressure,
            },
            "host-b": {
                "lanes": 0,
                "max_lanes": 10,
                "ram_mb": 0,
                "budget_ram_mb": 4958,
                "healthy": True,
                "pressure": None,
            },
        },
        "lanes_running": 1,
        "max_lanes": 5,
        "ledger_ram_mb": 5000,
        "budget_ram_mb": 11000,
        "disk_gb": {},
        "running": [{"class": "node", "state": "busy", "idle_seconds": None}],
        "deferred": [
            {
                "reason": "budget_full",
                "review": None,
                "gates_by_host": {"host-a": ["host_pressure"], "host-b": ["budget_full"]},
            }
        ],
        "admission_mode": "reservation_plus_guard",
        "review_sessions": 0,
        "review_state_reachable": True,
    }


def _run(payload: dict) -> str:
    buf = io.StringIO()
    with (
        patch.object(urllib.request, "urlopen", return_value=io.StringIO(json.dumps(payload))),
        redirect_stdout(buf),
    ):
        exec(compile(SCRIPT.read_text(), "ci_status.py", "exec"), {"__name__": "__main__"})
    return buf.getvalue()


def test_shows_the_pressure_and_every_hosts_gates() -> None:
    """2026-11-07: 5/11 GB reserved, nothing starting, and the one reason printed was
    another host's budget_full. The real hold, a saturated CPU, must show."""
    out = _run(_payload(PRESSURE))

    assert "load 27.9/16" in out
    assert "HOLDING new lanes (host_pressure)" in out
    assert "'host-a: host_pressure': 1" in out
    assert "'host-b: budget_full': 1" in out
    assert "6/15" not in out and "1/15" in out, "total lanes sum each host's own ceiling"


def test_names_a_pressure_payload_of_another_shape_instead_of_crashing() -> None:
    out = _run(_payload({"binding": True}))

    assert "unexpected shape" in out
