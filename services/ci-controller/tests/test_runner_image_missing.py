"""A reachable host whose runner image is missing takes no lanes, visibly.

A lane host's socket proxy denies IMAGES, so it can never pull a runner image it lacks;
after a pin bump changed `runner_image`, one host failed every spawn for 90 minutes while
the controller kept calling it healthy and retried it every tick."""

from __future__ import annotations

import logging

import pytest
from src.config import ControllerConfig
from src.docker_adapter import LaneInfo
from src.models import Reservation

from tests.two_host_fleet import IMAGE, TWO_HOST_CONFIG, Fleet


@pytest.fixture()
def fleet(write_config, monkeypatch) -> Fleet:
    return Fleet(write_config, monkeypatch)


def test_a_host_whose_image_cannot_be_pulled_is_benched_and_says_why(fleet, caplog) -> None:
    fleet.image_missing_on("host-a")
    fleet.queue(1, 2)

    with caplog.at_level(logging.WARNING, logger="ci-controller"):
        fleet.ctrl.tick()

    # The second job was planned onto host-a too; it is not sent into the same refusal.
    assert fleet.adapters["host-a"].spawn.call_count == 1
    assert fleet.host_status("host-a")["healthy"] is False
    assert fleet.host_status("host-a")["unhealthy_reason"] == "runner_image_missing"
    (warning,) = [r for r in caplog.records if "runner image" in r.getMessage()]
    assert "host-a" in warning.getMessage()
    assert IMAGE in warning.getMessage()
    assert "re-apply the ci_lane_host role at this pin on that host" in warning.getMessage()


def test_a_benched_host_is_not_offered_to_admission_so_work_goes_elsewhere(fleet) -> None:
    fleet.image_missing_on("host-a")
    fleet.queue(1)
    fleet.ctrl.tick()

    fleet.ctrl.tick()

    assert fleet.adapters["host-a"].spawn.call_count == 1
    assert fleet.adapters["host-b"].spawn.call_count == 1
    assert fleet.ledger.has_job(1)


def test_after_the_cooldown_one_spawn_re_tests_the_host(fleet, caplog) -> None:
    cooldown = fleet.config.runner_image_missing_cooldown_seconds
    assert cooldown == 600
    fleet.image_missing_on("host-a")
    fleet.up = {"host-a"}  # nowhere else to go, so every tick wants host-a
    fleet.queue(1)
    fleet.ctrl.tick()

    fleet.now += cooldown - 1
    fleet.ctrl.tick()
    assert fleet.adapters["host-a"].spawn.call_count == 1

    fleet.now += 1
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ci-controller"):
        fleet.ctrl.tick()
    assert fleet.adapters["host-a"].spawn.call_count == 2
    # Still missing: benched again, but not announced as a fresh outage.
    assert fleet.host_status("host-a")["unhealthy_reason"] == "runner_image_missing"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_host_that_gets_its_image_back_rejoins_and_says_so(fleet, caplog) -> None:
    fleet.image_missing_on("host-a")
    fleet.up = {"host-a"}
    fleet.queue(1)
    fleet.ctrl.tick()
    fleet.adapters["host-a"].spawn.side_effect = None
    fleet.adapters["host-a"].spawn.return_value = ("host-a-cici-1", "cid-1")

    fleet.now += fleet.config.runner_image_missing_cooldown_seconds
    with caplog.at_level(logging.WARNING, logger="ci-controller"):
        fleet.ctrl.tick()

    assert fleet.ledger.has_job(1)
    assert fleet.host_status("host-a")["unhealthy_reason"] is None
    assert fleet.host_status("host-a")["healthy"] is True
    assert any("back in admission" in r.getMessage() for r in caplog.records)


def test_a_benched_hosts_running_lanes_are_never_reaped(fleet) -> None:
    """Benching stops admissions only. The host still answers and lists its lanes, so
    the host_unhealthy_ticks debounce that reaps a lost host's lanes never starts."""
    fleet.ledger.add(
        Reservation(
            lane_id="host-a-cici-9",
            spawned_for_job_id=9,
            repo="ordago-app/ordago-apps",
            class_name="light",
            ram_mb=700,
            needs_kvm=False,
            work_disk="ssd",
            host="host-a",
        )
    )
    running = LaneInfo(lane_id="host-a-cici-9", job_id=9, container_id="c9", host="host-a")
    fleet.adapters["host-a"].list_lanes.return_value = [running]
    fleet.image_missing_on("host-a")
    fleet.queue(1)

    for _ in range(fleet.config.host_unhealthy_ticks + 2):
        fleet.ctrl.tick()

    assert "host-a-cici-9" in fleet.ledger.lane_ids("host-a")


def test_a_reachable_host_with_its_image_has_no_unhealthy_reason(fleet) -> None:
    fleet.queue(1)
    fleet.ctrl.tick()

    assert fleet.host_status("host-a")["healthy"] is True
    assert fleet.host_status("host-a")["unhealthy_reason"] is None


def test_a_host_that_does_not_answer_is_unreachable(fleet) -> None:
    fleet.up = {"host-a"}
    fleet.ctrl.tick()

    assert fleet.host_status("host-b")["healthy"] is False
    assert fleet.host_status("host-b")["unhealthy_reason"] == "unreachable"


def test_the_cooldown_must_be_positive(write_config) -> None:
    with pytest.raises(Exception, match="runner_image_missing_cooldown_seconds"):
        ControllerConfig.load(
            write_config(TWO_HOST_CONFIG + "runner_image_missing_cooldown_seconds: 0\n")
        )
