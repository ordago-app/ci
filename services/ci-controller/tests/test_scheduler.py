from __future__ import annotations

from src.config import ControllerConfig
from src.ledger import Ledger
from src.models import AdmitDecision, DeferDecision, QueuedJob
from src.scheduler import LocalScheduler

from tests.conftest import VALID_CONFIG


def _scheduler(write_config) -> LocalScheduler:
    config = ControllerConfig.load(write_config(VALID_CONFIG))
    return LocalScheduler(config=config, ledger=Ledger())


def test_plan_admits_a_light_job(write_config):
    sched = _scheduler(write_config)
    job = QueuedJob(job_id=1, repo="ordago-app/ordago-apps", labels=["ordago-ci"])

    decisions = sched.plan([job], host_stats={}, healthy=None)

    assert len(decisions) == 1
    assert isinstance(decisions[0], AdmitDecision)
    assert decisions[0].class_name == "light"


def test_commit_then_lanes_reports_the_reservation(write_config):
    sched = _scheduler(write_config)
    job = QueuedJob(job_id=1, repo="ordago-app/ordago-apps", labels=["ordago-ci"])
    decision = sched.plan([job], host_stats={}, healthy=None)[0]

    sched.commit(decision, lane_id="lane-a", container_id="c1", idle_since=100.0)

    lanes = sched.lanes()
    assert [r.lane_id for r in lanes] == ["lane-a"]
    assert lanes[0].spawned_for_job_id == 1
    assert lanes[0].container_id == "c1"


def test_committed_job_defers_as_already_running(write_config):
    sched = _scheduler(write_config)
    job = QueuedJob(job_id=1, repo="ordago-app/ordago-apps", labels=["ordago-ci"])
    sched.commit(
        sched.plan([job], {}, None)[0], lane_id="lane-a", container_id="c1", idle_since=1.0
    )

    again = sched.plan([job], host_stats={}, healthy=None)

    assert isinstance(again[0], DeferDecision)
    assert again[0].reason == "already_running"


def test_update_then_release(write_config):
    sched = _scheduler(write_config)
    job = QueuedJob(job_id=1, repo="ordago-app/ordago-apps", labels=["ordago-ci"])
    sched.commit(
        sched.plan([job], {}, None)[0], lane_id="lane-a", container_id="c1", idle_since=1.0
    )

    sched.update("lane-a", running_job_id=42, idle_since=None)
    assert sched.lanes()[0].running_job_id == 42

    sched.release("lane-a")
    assert sched.lanes() == []


def test_update_unknown_lane_raises(write_config):
    sched = _scheduler(write_config)
    try:
        sched.update("nope", running_job_id=1)
    except KeyError:
        return
    raise AssertionError("expected KeyError for an unknown lane")
