import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
from fastapi.testclient import TestClient
from src.api import _safe_tick, create_app
from src.job_store import ReviewJobStore
from src.worker import ReviewWorker

from tests.helpers import begin_attempt


@pytest.fixture()
def store(tmp_path: Path) -> ReviewJobStore:
    s = ReviewJobStore(tmp_path / "jobs.sqlite3")
    s.init()
    return s


@pytest.fixture()
def status_client(store: ReviewJobStore) -> TestClient:
    worker = MagicMock()
    worker.store = store
    # A real int, not the MagicMock the attribute would otherwise be: /status passes it
    # into a SQL comparison, and a mock binds as an unsupported type.
    worker.max_attempts = 3
    client = TestClient(create_app(worker))
    # The app closes over this mock, so a test that needs to drive /status has
    # no other handle on it.
    client.worker = worker
    return client


def test_safe_tick_swallows_tick_errors() -> None:
    # A single failing tick must not propagate — otherwise the poll loop dies
    # permanently and polling silently stops until the container restarts.
    worker = MagicMock()
    worker.tick.side_effect = RuntimeError("boom")
    asyncio.run(_safe_tick(worker))
    worker.tick.assert_called_once()


def test_post_reviews_enqueues_and_returns_at_once(status_client, store) -> None:
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 5, "deadbeef", "base")
    status_client.worker.request_review.return_value = job
    status_client.worker.reviewer_bot = "powerreviewer[bot]"

    resp = status_client.post("/reviews", json={"repo": "alvaro/homelab", "pr_number": 5})

    assert resp.status_code == 202
    body = resp.json()
    assert (body["head_sha"], body["status"], body["done"]) == ("deadbeef", "queued", False)
    status_client.worker.request_review.assert_called_once_with("alvaro/homelab", 5)


def test_post_reviews_404s_for_a_repo_with_no_policy(status_client) -> None:
    status_client.worker.request_review.side_effect = KeyError("no policy for 'x/y'")
    resp = status_client.post("/reviews", json={"repo": "x/y", "pr_number": 1})
    assert resp.status_code == 404


def test_get_review_state_reads_the_head_from_the_store(status_client, store) -> None:
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 5, "deadbeef", "base")
    begin_attempt(store, job.id)
    store.mark_posted(job.id, verdict="APPROVE", provider="claude-work")

    body = status_client.get("/reviews/alvaro/homelab/5/deadbeef").json()

    assert body["done"] is True
    assert body["verdict"] == "APPROVE"
    assert body["provider"] == "claude-work"
    assert body["budget_exhausted"] is False


def test_get_review_state_404s_for_an_unrequested_head(status_client) -> None:
    assert status_client.get("/reviews/alvaro/homelab/5/nope").status_code == 404


def test_a_budget_skip_is_done_and_says_so(status_client, store) -> None:
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 5, "deadbeef", "base")
    store.mark_skipped(job.id, "max review rounds (5) reached")

    body = status_client.get("/reviews/alvaro/homelab/5/deadbeef").json()

    assert (body["done"], body["budget_exhausted"], body["verdict"]) == (True, True, None)


def test_a_head_moved_skip_is_not_a_budget_skip(status_client, store) -> None:
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 5, "deadbeef", "base")
    store.mark_skipped(job.id, "head moved to cafe")

    body = status_client.get("/reviews/alvaro/homelab/5/deadbeef").json()

    assert (body["done"], body["budget_exhausted"]) == (True, False)


def test_a_failure_with_retries_left_is_not_done(status_client, store) -> None:
    job = store.enqueue("alvaro/homelab", "homelab", "codex", 5, "deadbeef", "base")
    begin_attempt(store, job.id)
    store.mark_failed(job.id, "transient")
    assert status_client.get("/reviews/alvaro/homelab/5/deadbeef").json()["done"] is False

    for _ in range(2):
        begin_attempt(store, job.id)
        store.mark_failed(job.id, "boom")
    assert status_client.get("/reviews/alvaro/homelab/5/deadbeef").json()["done"] is True


def test_an_enqueue_wakes_the_poll_loop_instead_of_waiting_out_the_interval(store) -> None:
    worker = MagicMock()
    worker.store = store
    worker.max_attempts = 3
    worker.reviewer_bot = "bot"
    worker.request_review.return_value = store.enqueue("o/r", "r", "codex", 1, "a" * 40, "b")
    with TestClient(create_app(worker, poll_interval=3600)) as client:
        deadline = time.time() + 5
        while worker.tick.call_count < 1 and time.time() < deadline:
            time.sleep(0.01)
        assert worker.tick.call_count == 1, "the loop ticks once at startup"

        client.post("/reviews", json={"repo": "o/r", "pr_number": 1})

        deadline = time.time() + 5
        while worker.tick.call_count < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert worker.tick.call_count == 2, "the enqueue must not wait an hour for the next tick"


def test_healthz() -> None:
    client = TestClient(create_app(MagicMock()))
    assert client.get("/healthz").json() == {"status": "ok"}


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _CountingTicks:
    """A worker.tick that counts, and can be made to hang from a given tick on."""

    def __init__(self, *, raises: bool = False, hang_from: int | None = None) -> None:
        self.started = 0
        self.raises = raises
        self.hang_from = hang_from
        self.release = threading.Event()

    def __call__(self) -> None:
        self.started += 1
        if self.hang_from is not None and self.started >= self.hang_from:
            self.release.wait(10)
        if self.raises:
            raise RuntimeError("github down")


def _until_ticks(ticks: _CountingTicks, count: int) -> None:
    deadline = time.time() + 10
    while ticks.started < count and time.time() < deadline:
        time.sleep(0.01)
    assert ticks.started >= count


def _health_app(store, ticks: _CountingTicks, clock: _FakeClock):
    worker = MagicMock()
    worker.store = store
    worker.tick.side_effect = ticks
    # poll_interval=1: unhealthy after 5 s of clock without a finished tick.
    return create_app(worker, poll_interval=1, clock=clock)


@pytest.mark.parametrize("raises", [False, True], ids=["ok-ticks", "failing-ticks"])
def test_each_finished_tick_keeps_the_service_healthy(store, raises: bool) -> None:
    """A tick that raises is a GitHub problem, already logged: the loop is alive.
    Measured past the startup grace, so only the tick's heartbeat can pass it."""
    clock, ticks = _FakeClock(), _CountingTicks(raises=raises)
    with TestClient(_health_app(store, ticks, clock)) as client:
        _until_ticks(ticks, 1)
        clock.now = 1000.0
        # Tick 3 starting means tick 2, which began after the jump, has finished.
        _until_ticks(ticks, 3)

        assert client.get("/healthz").status_code == 200


def test_health_measures_from_the_last_finished_tick(store) -> None:
    """Healthy up to five intervals of silence, unhealthy past it, and healthy
    again the moment the stalled tick finishes."""
    clock, ticks = _FakeClock(), _CountingTicks(hang_from=2)
    with TestClient(_health_app(store, ticks, clock)) as client:
        _until_ticks(ticks, 2)  # tick 1 finished at 0.0; tick 2 is now hanging

        clock.now = 5.0
        assert client.get("/healthz").status_code == 200
        clock.now = 5.5
        stalled = client.get("/healthz")

        ticks.hang_from = None
        ticks.release.set()
        _until_ticks(ticks, 3)  # tick 2 finished, its heartbeat at 5.5
        clock.now = 10.0
        recovered = client.get("/healthz")

    assert stalled.status_code == 503
    assert "poll loop silent for 5s" in stalled.json()["detail"]
    assert recovered.status_code == 200


def test_app_starts_without_background_poll() -> None:
    # poll_interval=0 -> no background task; app still serves.
    with TestClient(create_app(MagicMock(), poll_interval=0)) as client:
        assert client.get("/healthz").status_code == 200


def test_status_reports_counts_and_active_jobs(status_client, store) -> None:
    store.enqueue("o/r", "r", "codex", 7, "a" * 40, "b" * 40)
    job = store.enqueue("o/r", "r", "codex", 8, "c" * 40, "d" * 40)
    begin_attempt(store, job.id)

    body = status_client.get("/status").json()

    assert body["counts"]["queued"] == 1
    assert body["counts"]["running"] == 1
    assert body["counts"]["failed"] == 0
    assert {j["pr_number"] for j in body["active"]} == {7, 8}
    assert all("last_error" in j for j in body["active"])


def test_status_reports_spend_per_account(status_client, store) -> None:
    job = store.enqueue("o/r", "r", "claude-a", 7, "a" * 40, "b" * 40)
    store.record_provider_run(job.id, "claude-a", 0.42)

    spend = status_client.get("/status").json()["spend"]

    for window in ("day", "month"):
        assert spend[window] == [
            {"account": "claude-a", "runs": 1, "priced_runs": 1, "cost_usd": 0.42}
        ]


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


def test_status_reports_stuck_reviews_separately_from_the_lifetime_total(
    status_client, store
) -> None:
    """The dashboard's attention badge reads `stuck`. counts["failed"] cannot serve that
    role: it is a lifetime total, so it never falls back to zero once anything has ever
    failed, and a badge that is permanently lit is one nobody reads."""
    retryable = store.enqueue("o/r", "r", "codex", 1, "a" * 40, "b" * 40)
    begin_attempt(store, retryable.id)  # attempts -> 1, still under the limit
    store.mark_failed(retryable.id, "transient")

    exhausted = store.enqueue("o/r", "r", "codex", 2, "c" * 40, "d" * 40)
    for _ in range(3):
        begin_attempt(store, exhausted.id)
        store.mark_failed(exhausted.id, "boom")

    body = status_client.get("/status").json()

    assert body["counts"]["failed"] == 2  # both, for all time
    assert body["stuck"] == 1  # only the one nothing will retry


def test_status_reports_no_stuck_reviews_on_a_healthy_store(status_client, store) -> None:
    store.enqueue("o/r", "r", "codex", 3, "e" * 40, "f" * 40)
    assert status_client.get("/status").json()["stuck"] == 0


def test_status_carries_the_reviewer_quota_rows(status_client) -> None:
    """The dashboard's Reviewer quota card reads this field and nothing else.

    Asserted at the endpoint, not only on the worker: the worker builds the rows
    and the dashboard normalizes them, but neither notices if the `/status` line
    carrying them between the two is dropped. The card would render empty
    against a service reporting itself perfectly healthy — the quiet failure
    this whole change exists to end.
    """
    rows = [
        {"provider": "codex", "quota": None, "outage": None, "last_ok": 1788516000.0},
        {
            "provider": "claude-work",
            "quota": {
                "account": "claude-work",
                "status": "allowed_warning",
                "limit_type": "overage",
                "utilization": 18.67,
                "resets_at": 1790812800.0,
                "observed_at": 1788516000.0,
            },
            "outage": None,
            "last_ok": 1788516000.0,
        },
    ]
    status_client.worker.reviewer_quota.return_value = rows

    body = status_client.get("/status").json()

    assert body["reviewer_quota"] == rows


def test_status_serves_the_quota_rows_verbatim(status_client) -> None:
    """No filtering, reshaping or defaulting on the way out. Every nullable
    field means something specific — a null `quota` is 'this provider has no
    quota API', a null `utilization` is 'the CLI reported no number' — and an
    endpoint that helpfully dropped or zero-filled either would hand the card a
    number nobody measured."""
    status_client.worker.reviewer_quota.return_value = [
        {"provider": "codex", "quota": None, "outage": None, "last_ok": None}
    ]

    row = status_client.get("/status").json()["reviewer_quota"][0]

    assert row == {"provider": "codex", "quota": None, "outage": None, "last_ok": None}


def test_status_does_not_report_an_old_failure_as_stuck(status_client, store) -> None:
    """End-to-end version of the bug the window exists for: a terminal failure from two
    months ago is history, not a task, and must not keep the badge lit. Shipped without
    the window, production reported 15 of exactly this shape."""
    old = store.enqueue("o/r", "r", "codex", 9, "e" * 40, "f" * 40)
    for _ in range(3):
        begin_attempt(store, old.id)
        store.mark_failed(old.id, "boom")
    with sqlite3.connect(store._path) as conn:
        stale = time.time() - 50 * 24 * 3600
        conn.execute(
            "UPDATE review_jobs SET finished_at = ?, queued_at = ? WHERE id = ?",
            (stale, stale, old.id),
        )

    body = status_client.get("/status").json()

    assert body["counts"]["failed"] == 1  # the lifetime total still carries it
    assert body["stuck"] == 0  # ...and the live signal does not


def test_the_stuck_window_is_configurable(store) -> None:
    """So the threshold is a decision at the edge, not a constant buried in a query."""
    worker = MagicMock()
    worker.store = store
    worker.max_attempts = 3
    job = store.enqueue("o/r", "r", "codex", 10, "g" * 40, "h" * 40)
    for _ in range(3):
        begin_attempt(store, job.id)
        store.mark_failed(job.id, "boom")

    wide = TestClient(create_app(worker, stuck_window_seconds=3600))
    narrow = TestClient(create_app(worker, stuck_window_seconds=0))

    assert wide.get("/status").json()["stuck"] == 1
    assert narrow.get("/status").json()["stuck"] == 0


def test_the_poll_loop_starts_the_review_runners_first(store) -> None:
    worker = MagicMock()
    worker.store = store
    with TestClient(create_app(worker, poll_interval=3600)):
        deadline = time.time() + 5
        while worker.tick.call_count < 1 and time.time() < deadline:
            time.sleep(0.01)
    worker.start.assert_called_once_with()
    assert worker.method_calls.index(call.start()) < worker.method_calls.index(call.tick())


def _heads(client: TestClient, *shas: str, repo: str = "alvaro/homelab") -> dict:
    return client.get("/heads", params={"repo": repo, "sha": list(shas)}).json()["heads"]


def test_heads_maps_every_review_outcome_to_one_state(status_client, store) -> None:
    def job(pr: int, sha: str) -> int:
        return store.enqueue("alvaro/homelab", "homelab", "codex", pr, sha, "base").id

    job(1, "queued")
    begin_attempt(store, job(2, "running"))
    approved = job(3, "approved")
    store.mark_posted(approved, verdict="APPROVE")
    rejected = job(4, "rejected")
    store.mark_posted(rejected, verdict="REQUEST_CHANGES")
    legacy = job(5, "legacy")
    store.mark_posted(legacy)
    retrying = job(6, "retrying")
    begin_attempt(store, retrying)
    store.mark_failed(retrying, "transient")
    exhausted = job(7, "exhausted")
    for _ in range(3):
        begin_attempt(store, exhausted)
        store.mark_failed(exhausted, "boom")
    store.mark_skipped(job(8, "skipped"), "head moved to cafe")

    assert _heads(
        status_client,
        "queued",
        "running",
        "approved",
        "rejected",
        "legacy",
        "retrying",
        "exhausted",
        "skipped",
    ) == {
        "queued": "pending",
        "running": "running",
        "approved": "approved",
        "rejected": "changes_requested",
        "legacy": "changes_requested",
        "retrying": "pending",
        "exhausted": "failed",
        "skipped": "skipped",
    }


def test_heads_leaves_out_heads_it_has_never_seen_and_other_repos(status_client, store) -> None:
    store.enqueue("alvaro/other", "other", "codex", 1, "elsewhere", "base")

    assert _heads(status_client, "elsewhere", "unknown") == {}


def test_heads_reads_the_newest_review_of_a_head(status_client, store) -> None:
    first = store.enqueue("alvaro/homelab", "homelab", "codex", 1, "shared", "base")
    store.mark_posted(first.id, verdict="REQUEST_CHANGES")
    second = store.enqueue("alvaro/homelab", "homelab", "codex", 2, "shared", "base")
    store.mark_posted(second.id, verdict="APPROVE")

    assert _heads(status_client, "shared") == {"shared": "approved"}


def test_heads_requires_at_least_one_sha(status_client) -> None:
    assert status_client.get("/heads", params={"repo": "alvaro/homelab"}).status_code == 422
