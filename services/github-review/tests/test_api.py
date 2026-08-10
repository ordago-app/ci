import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from src.api import _safe_tick, create_app
from src.job_store import ReviewJobStore
from src.worker import ReviewResultSummary, ReviewWorker


@pytest.fixture()
def store(tmp_path: Path) -> ReviewJobStore:
    s = ReviewJobStore(tmp_path / "jobs.sqlite3")
    s.init()
    return s


@pytest.fixture()
def status_client(store: ReviewJobStore) -> TestClient:
    worker = MagicMock()
    worker.store = store
    return TestClient(create_app(worker))


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


def test_status_reports_counts_and_active_jobs(status_client, store) -> None:
    store.enqueue("o/r", "r", "codex", 7, "a" * 40, "b" * 40)
    job = store.enqueue("o/r", "r", "codex", 8, "c" * 40, "d" * 40)
    store.mark_running(job.id)

    body = status_client.get("/status").json()

    assert body["counts"]["queued"] == 1
    assert body["counts"]["running"] == 1
    assert body["counts"]["failed"] == 0
    assert {j["pr_number"] for j in body["active"]} == {7, 8}
    assert all("last_error" in j for j in body["active"])


def test_status_surfaces_last_error_and_drops_terminal_jobs(status_client, store) -> None:
    job = store.enqueue("o/r", "r", "codex", 9, "e" * 40, "f" * 40)
    store.mark_failed(job.id, "codex exited 1")

    body = status_client.get("/status").json()

    assert body["counts"]["failed"] == 1
    # failed is terminal for the *active* list; the count still reports it.
    assert body["active"] == []


def test_status_zero_fills_every_status_on_an_empty_store(status_client) -> None:
    counts = status_client.get("/status").json()["counts"]
    assert counts == {"queued": 0, "running": 0, "posted": 0, "skipped": 0, "failed": 0}


def test_worker_exposes_its_store_publicly() -> None:
    # /status reads worker.store; without this property it would have to reach
    # into the private attribute and would break silently on a rename.
    store = MagicMock()
    worker = ReviewWorker(
        config=MagicMock(),
        store=store,
        github=MagicMock(),
        worktrees=MagicMock(),
        providers={},
        projects_root=Path("/tmp"),
        reviewer_bot="bot",
    )
    assert worker.store is store
