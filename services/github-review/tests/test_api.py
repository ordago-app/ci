import asyncio
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from src.api import _safe_tick, create_app
from src.worker import ReviewResultSummary


def test_safe_tick_swallows_tick_errors() -> None:
    # A single failing tick must not propagate — otherwise the poll loop dies
    # permanently and polling silently stops until the container restarts.
    worker = MagicMock()
    worker.tick.side_effect = RuntimeError("boom")
    asyncio.run(_safe_tick(worker, asyncio.Lock()))
    worker.tick.assert_called_once()


def test_post_reviews_runs_and_returns_verdict() -> None:
    worker = MagicMock()
    worker.run_pr_review.return_value = ReviewResultSummary("deadbeef", "APPROVE", False)
    worker.reviewer_bot = "powerreviewer[bot]"
    client = TestClient(create_app(worker))

    resp = client.post("/reviews", json={"repo": "alvaro/homelab", "pr_number": 5})

    assert resp.status_code == 200
    assert resp.json() == {
        "head_sha": "deadbeef",
        "verdict": "APPROVE",
        "escalated": False,
        "reviewer": "powerreviewer[bot]",
    }
    worker.run_pr_review.assert_called_once_with("alvaro/homelab", 5)


def test_healthz() -> None:
    client = TestClient(create_app(MagicMock()))
    assert client.get("/healthz").json() == {"status": "ok"}


def test_app_starts_without_background_poll() -> None:
    # poll_interval=0 -> no background task; app still serves.
    with TestClient(create_app(MagicMock(), poll_interval=0)) as client:
        assert client.get("/healthz").status_code == 200


def test_post_reviews_returns_503_on_worker_error() -> None:
    worker = MagicMock()
    worker.run_pr_review.side_effect = RuntimeError("review did not complete: boom")
    worker.reviewer_bot = "powerreviewer[bot]"
    client = TestClient(create_app(worker), raise_server_exceptions=False)

    resp = client.post("/reviews", json={"repo": "alvaro/homelab", "pr_number": 5})

    assert resp.status_code == 503
    assert "review did not complete: boom" in resp.json()["detail"]
