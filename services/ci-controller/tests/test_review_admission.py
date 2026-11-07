"""Review tiers in admission (ADR 0032): who gets a lane when lanes are scarce."""

from src.admission import evaluate
from src.config import ControllerConfig
from src.ledger import Ledger
from src.models import (
    BUDGET_FULL,
    REVIEW_APPROVED,
    REVIEW_CHANGES_REQUESTED,
    REVIEW_FAILED,
    REVIEW_HELD,
    REVIEW_PENDING,
    REVIEW_RUNNING,
    AdmitDecision,
    Decision,
    DeferDecision,
    QueuedJob,
    Reservation,
)

REPO = "alvaro-francisco-gil/homelab"

# 3000 MB on powerserver: room for one node (2000) plus one light (700), or four lights.
TIER_CONFIG = """\
ram_budget_mb: 3000
max_concurrent_lanes: 10
default_class: light
default_host: powerserver
runner_image: homelab/github-actions-runner:latest
work_dirs:
  ssd: /mnt/ci-ssd/ci-controller
  hdd: /opt/personal/github-actions/ci-controller
classes:
  light:
    ram_mb: 700
  node:
    ram_mb: 2000
review_admission:
  cheap_classes: [light]
  session_host: powerserver
  session_ram_mb: 600
repos:
  - project: homelab
    label_class:
      homelab: light
      node: node
"""


def _cfg(write_config) -> ControllerConfig:
    return ControllerConfig.load(write_config(TIER_CONFIG))


def _job(
    job_id: int, review: str | None, *, label: str = "homelab", pinned: bool = True
) -> QueuedJob:
    run_id = 1000 + job_id
    labels = ["self-hosted", label] + ([f"run-{run_id}"] if pinned else [])
    return QueuedJob(
        job_id=job_id,
        repo=REPO,
        labels=labels,
        run_id=run_id,
        head_sha=f"sha{job_id}",
        review=review,
    )


def _verdicts(decisions: list[Decision]) -> dict[int, str]:
    return {
        d.job.job_id: ("admit" if isinstance(d, AdmitDecision) else d.reason) for d in decisions
    }


def test_an_approved_head_takes_the_last_lane_ahead_of_one_under_review(write_config) -> None:
    ledger = Ledger()
    ledger.add(Reservation("busy", 99, REPO, "light", 2300, False, host="powerserver"))

    decisions = evaluate(
        [_job(1, REVIEW_PENDING), _job(2, REVIEW_APPROVED)], ledger, _cfg(write_config)
    )

    assert _verdicts(decisions) == {2: "admit", 1: BUDGET_FULL}


def test_a_head_with_no_review_row_ranks_with_approved(write_config) -> None:
    """No reviewer on the repo, an unlabelled PR, a branch push: nothing to wait for."""
    ledger = Ledger()
    ledger.add(Reservation("busy", 99, REPO, "light", 2300, False, host="powerserver"))

    decisions = evaluate([_job(1, REVIEW_RUNNING), _job(2, None)], ledger, _cfg(write_config))

    assert _verdicts(decisions) == {2: "admit", 1: BUDGET_FULL}


def test_a_failed_review_never_holds_its_head_back(write_config) -> None:
    ledger = Ledger()
    ledger.add(Reservation("busy", 99, REPO, "light", 2300, False, host="powerserver"))

    decisions = evaluate(
        [_job(1, REVIEW_PENDING), _job(2, REVIEW_FAILED)], ledger, _cfg(write_config)
    )

    assert _verdicts(decisions) == {2: "admit", 1: BUDGET_FULL}


def test_an_unpinned_job_is_not_tiered_because_the_order_could_not_bind(write_config) -> None:
    """Without its run label GitHub hands the lane any job with the same labels; ordering
    it by review state would be a guess. It keeps the pre-tier largest-first order."""
    ledger = Ledger()
    ledger.add(Reservation("busy", 99, REPO, "light", 2300, False, host="powerserver"))
    rejected_unpinned = _job(1, REVIEW_CHANGES_REQUESTED, pinned=False)

    decisions = evaluate([rejected_unpinned, _job(2, REVIEW_PENDING)], ledger, _cfg(write_config))

    assert _verdicts(decisions) == {1: "admit", 2: BUDGET_FULL}


def test_under_review_cheap_classes_go_before_heavy_ones(write_config) -> None:
    """Largest-first would give node the machine; among heads still under review the
    cheap lint/unit lanes go first, so their failures land in the same fix round."""
    decisions = evaluate(
        [_job(1, REVIEW_PENDING, label="node"), *(_job(n, REVIEW_PENDING) for n in (2, 3, 4))],
        Ledger(),
        _cfg(write_config),
    )

    assert _verdicts(decisions) == {2: "admit", 3: "admit", 4: "admit", 1: BUDGET_FULL}


def test_largest_first_still_orders_within_a_tier(write_config) -> None:
    decisions = evaluate(
        [*(_job(n, REVIEW_APPROVED) for n in (2, 3, 4)), _job(1, REVIEW_APPROVED, label="node")],
        Ledger(),
        _cfg(write_config),
    )

    assert _verdicts(decisions) == {1: "admit", 2: "admit", 3: BUDGET_FULL, 4: BUDGET_FULL}


def test_a_rejected_head_waits_while_anything_above_it_waits(write_config) -> None:
    """Room for the rejected light job exists, but a node job under review is waiting on
    budget: the rejected head is held rather than spending the space."""
    ledger = Ledger()
    ledger.add(Reservation("busy", 99, REPO, "light", 1500, False, host="powerserver"))

    decisions = evaluate(
        [_job(1, REVIEW_CHANGES_REQUESTED), _job(2, REVIEW_PENDING, label="node")],
        ledger,
        _cfg(write_config),
    )

    assert _verdicts(decisions) == {2: BUDGET_FULL, 1: REVIEW_HELD}
    held = next(d for d in decisions if d.job.job_id == 1)
    assert isinstance(held, DeferDecision) and held.class_name == "light"


def test_a_rejected_head_runs_when_nothing_else_waits(write_config) -> None:
    """Never idle: with free lanes a rejected head's CI runs, so the author fixes CI
    failures and findings in one round."""
    decisions = evaluate(
        [_job(1, REVIEW_CHANGES_REQUESTED), _job(2, REVIEW_PENDING)], Ledger(), _cfg(write_config)
    )

    assert _verdicts(decisions) == {1: "admit", 2: "admit"}


def test_a_non_capacity_defer_above_does_not_hold_a_rejected_head(write_config) -> None:
    """A job already running, or one no host may take, is not waiting for a lane."""
    ledger = Ledger()
    ledger.add(Reservation("lane", 2, REPO, "light", 700, False, host="powerserver"))

    decisions = evaluate(
        [_job(1, REVIEW_CHANGES_REQUESTED), _job(2, REVIEW_PENDING)], ledger, _cfg(write_config)
    )

    assert _verdicts(decisions) == {2: "already_running", 1: "admit"}


def test_review_containers_are_charged_to_the_session_host(write_config) -> None:
    """Two reviews x 600 MB leave 1800 of 3000: one light fits, two do not."""
    decisions = evaluate(
        [_job(1, None), _job(2, None), _job(3, None)],
        Ledger(),
        _cfg(write_config),
        review_sessions=2,
    )

    assert _verdicts(decisions) == {1: "admit", 2: "admit", 3: BUDGET_FULL}
