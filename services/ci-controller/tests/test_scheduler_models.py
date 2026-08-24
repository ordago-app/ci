from __future__ import annotations

from src.models import AdmitDecision, DeferDecision, QueuedJob, Reservation
from src.scheduler_models import (
    decision_from_wire,
    decision_to_wire,
    reservation_from_wire,
    reservation_to_wire,
)


def test_admit_decision_round_trips():
    job = QueuedJob(job_id=7, repo="o/r", labels=["ordago-ci"], workflow="ci", job_name="build")
    decision = AdmitDecision(
        job=job, class_name="light", ram_mb=700, needs_kvm=False, host="powerserver"
    )

    assert decision_from_wire(decision_to_wire(decision)) == decision


def test_defer_decision_round_trips_with_all_reasons():
    job = QueuedJob(job_id=8, repo="o/r", labels=[])
    decision = DeferDecision(
        job, ("budget_full", "lane_ceiling"), host="powerserver", class_name="light"
    )

    restored = decision_from_wire(decision_to_wire(decision))

    assert restored == decision
    assert restored.reasons == ("budget_full", "lane_ceiling")


def test_reservation_round_trips_including_none_fields():
    res = Reservation(
        lane_id="lane-a",
        spawned_for_job_id=1,
        repo="o/r",
        class_name="light",
        ram_mb=700,
        needs_kvm=False,
        started_at=None,
        running_job_id=None,
        idle_since=12.5,
    )

    assert reservation_from_wire(reservation_to_wire(res)) == res
