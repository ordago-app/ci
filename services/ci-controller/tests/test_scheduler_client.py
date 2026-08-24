from __future__ import annotations

import httpx
import pytest
from src.models import AdmitDecision, QueuedJob, Reservation
from src.scheduler_client import HttpScheduler, SchedulerUnavailable


def _client(handler) -> HttpScheduler:
    transport = httpx.MockTransport(handler)
    return HttpScheduler("http://sched:8001", transport=transport, retries=3, backoff=0.0)


def test_plan_parses_decisions():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "decisions": [
                    {
                        "kind": "admit",
                        "job": {
                            "job_id": 1,
                            "repo": "o/r",
                            "labels": [],
                            "workflow": "",
                            "job_name": "",
                        },
                        "class_name": "light",
                        "ram_mb": 700,
                        "needs_kvm": False,
                        "work_disk": "ssd",
                        "work_gb": 0,
                        "host": "powerserver",
                    }
                ]
            },
        )

    decisions = _client(handler).plan([QueuedJob(job_id=1, repo="o/r")], {}, None)

    assert isinstance(decisions[0], AdmitDecision)
    assert decisions[0].host == "powerserver"


def test_retries_then_succeeds():
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"decisions": []})

    assert _client(handler).plan([], {}, None) == []
    assert len(attempts) == 3


def test_raises_after_exhausting_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(SchedulerUnavailable):
        _client(handler).plan([], {}, None)


def test_update_unknown_lane_raises_keyerror():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "unknown lane"})

    with pytest.raises(KeyError):
        _client(handler).update("nope", running_job_id=1)


def test_404_is_not_retried():
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(404, json={"detail": "unknown lane"})

    with pytest.raises(KeyError):
        _client(handler).update("nope", running_job_id=1)

    assert len(attempts) == 1


def test_503_is_retried_then_succeeds():
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, json={"detail": "restarting"})
        return httpx.Response(200, json={"decisions": []})

    assert _client(handler).plan([], {}, None) == []
    assert len(attempts) == 3


def test_503_exhausts_retries_and_raises_scheduler_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "restarting"})

    with pytest.raises(SchedulerUnavailable):
        _client(handler).plan([], {}, None)


def test_400_is_not_retried():
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(400, json={"detail": "bad request"})

    with pytest.raises(httpx.HTTPStatusError):
        _client(handler).plan([], {}, None)

    assert len(attempts) == 1


def test_commit_is_not_retried():
    """A retried POST /lanes can hit a reservation the scheduler already committed, whose
    duplicate lane_id is a 500 the caller reads as "the lane was never reserved"."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ReadTimeout("lost the response", request=request)

    decision = AdmitDecision(
        job=QueuedJob(job_id=1, repo="o/r"),
        class_name="light",
        ram_mb=700,
        needs_kvm=False,
        work_disk="ssd",
        work_gb=0,
        host="powerserver",
    )
    with pytest.raises(SchedulerUnavailable):
        _client(handler).commit(decision, lane_id="lane-a", container_id="c1", idle_since=1.0)

    assert len(attempts) == 1


def test_adopt_is_not_retried():
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500, json={"detail": "boom"})

    with pytest.raises(SchedulerUnavailable):
        _client(handler).adopt(
            Reservation(
                lane_id="lane-a",
                spawned_for_job_id=1,
                repo="o/r",
                class_name="light",
                ram_mb=700,
                needs_kvm=False,
                work_disk="ssd",
                work_gb=0,
                workflow="",
                job_name="",
                host="powerserver",
            )
        )

    assert len(attempts) == 1
