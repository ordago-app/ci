from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from src.api import create_app
from src.worker import ReviewResultSummary


def test_post_reviews_runs_and_returns_verdict() -> None:
    worker = MagicMock()
    worker.run_pr_review.return_value = ReviewResultSummary("deadbeef", "APPROVE", False)
    client = TestClient(create_app(worker))

    resp = client.post("/reviews", json={"repo": "alvaro/homelab", "pr_number": 5})

    assert resp.status_code == 200
    assert resp.json() == {"head_sha": "deadbeef", "verdict": "APPROVE", "escalated": False}
    worker.run_pr_review.assert_called_once_with("alvaro/homelab", 5)


def test_healthz() -> None:
    client = TestClient(create_app(MagicMock()))
    assert client.get("/healthz").json() == {"status": "ok"}


def test_app_starts_without_background_poll() -> None:
    # poll_interval=0 -> no background task; app still serves.
    with TestClient(create_app(MagicMock(), poll_interval=0)) as client:
        assert client.get("/healthz").status_code == 200
