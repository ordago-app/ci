"""Host RAM holds: RAM an operator sets aside on a pool host for work the pool does not
schedule, charged against that host's budget so new lanes stop landing there."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from src.admission import evaluate
from src.config import ControllerConfig
from src.holds import Hold, HoldBook
from src.ledger import Ledger
from src.models import BUDGET_FULL, AdmitDecision, DeferDecision, QueuedJob, Reservation
from src.scheduler import LocalScheduler
from src.scheduler_api import create_scheduler_app
from src.scheduler_client import HttpScheduler

from tests.test_scheduler_parity import _TestClientTransport
from tests.two_host_fleet import TWO_HOST_CONFIG, Clock, Fleet


def _job(job_id: int, label: str = "ordago-ci") -> QueuedJob:
    return QueuedJob(job_id=job_id, repo="ordago-app/ordago-apps", labels=[label])


@pytest.fixture()
def config(write_config) -> ControllerConfig:
    return ControllerConfig.load(write_config(TWO_HOST_CONFIG))


# --- the book -----------------------------------------------------------------------------


def test_a_hold_expires_after_its_ttl_and_is_then_neither_listed_nor_charged() -> None:
    clock = Clock()
    book = HoldBook(clock)
    book.put("release", host="host-a", ram_mb=9000, ttl_seconds=600)

    clock.now += 599
    assert [h.name for h in book.active()] == ["release"]
    assert book.held_ram_mb() == {"host-a": 9000}

    clock.now += 1
    assert book.active() == []
    assert book.held_ram_mb() == {}


def test_re_putting_a_hold_renews_and_resizes_it() -> None:
    clock = Clock()
    book = HoldBook(clock)
    book.put("release", host="host-a", ram_mb=9000, ttl_seconds=600)
    clock.now += 500

    book.put("release", host="host-a", ram_mb=4000, ttl_seconds=600)

    (hold,) = book.active()
    assert hold == Hold(name="release", host="host-a", ram_mb=4000, expires_at=clock.now + 600)


def test_holds_on_one_host_add_up() -> None:
    book = HoldBook(Clock())
    book.put("a", host="host-a", ram_mb=1000, ttl_seconds=600)
    book.put("b", host="host-a", ram_mb=2000, ttl_seconds=600)
    book.put("c", host="host-b", ram_mb=500, ttl_seconds=600)

    assert book.held_ram_mb() == {"host-a": 3000, "host-b": 500}


@pytest.mark.parametrize(
    ("ram_mb", "ttl_seconds"),
    [(0, 600), (-1, 600), (1000, 59), (1000, 43201)],
    ids=["zero-ram", "negative-ram", "ttl-too-short", "ttl-too-long"],
)
def test_the_book_refuses_a_hold_out_of_range(ram_mb: int, ttl_seconds: int) -> None:
    with pytest.raises(ValueError):
        HoldBook(Clock()).put("x", host="host-a", ram_mb=ram_mb, ttl_seconds=ttl_seconds)


# --- admission ----------------------------------------------------------------------------


def test_a_hold_binds_budget_full_when_it_and_the_class_exceed_the_budget(config) -> None:
    (decision,) = evaluate(
        [_job(1, "heavy")],
        Ledger(),
        config,
        healthy={"host-a"},
        held_ram_mb={"host-a": 7500},
    )

    assert isinstance(decision, DeferDecision)
    assert decision.reasons == (BUDGET_FULL,)


def test_a_hold_that_leaves_room_still_admits(config) -> None:
    (decision,) = evaluate(
        [_job(1, "heavy")],
        Ledger(),
        config,
        healthy={"host-a"},
        held_ram_mb={"host-a": 7000},
    )

    assert isinstance(decision, AdmitDecision)
    assert decision.host == "host-a"


def test_a_hold_adds_to_the_lanes_already_reserved(config) -> None:
    ledger = Ledger()
    ledger.add(
        Reservation(
            lane_id="l1",
            spawned_for_job_id=9,
            repo="ordago-app/ordago-apps",
            class_name="heavy",
            ram_mb=4000,
            needs_kvm=False,
            work_disk="ssd",
            host="host-a",
        )
    )

    (decision,) = evaluate(
        [_job(1, "heavy")], ledger, config, healthy={"host-a"}, held_ram_mb={"host-a": 3500}
    )

    assert isinstance(decision, DeferDecision)
    assert BUDGET_FULL in decision.reasons


def test_a_hold_moves_placement_to_another_host_and_leaves_that_hosts_budget_alone(
    config,
) -> None:
    (decision,) = evaluate(
        [_job(1, "heavy")], Ledger(), config, healthy=None, held_ram_mb={"host-a": 10000}
    )

    assert isinstance(decision, AdmitDecision)
    assert decision.host == "host-b"


def test_the_scheduler_charges_its_own_holds_when_planning(config) -> None:
    sched = LocalScheduler(config, Ledger(), clock=Clock())
    sched.put_hold("release", host="host-a", ram_mb=10500, ttl_seconds=600)

    (decision,) = sched.plan([_job(1)], {}, {"host-a"}, review_sessions=0, ram_offers={})

    assert isinstance(decision, DeferDecision)
    assert decision.reasons == (BUDGET_FULL,)


def test_an_expired_hold_stops_binding(config) -> None:
    clock = Clock()
    sched = LocalScheduler(config, Ledger(), clock=clock)
    sched.put_hold("release", host="host-a", ram_mb=10500, ttl_seconds=600)
    clock.now += 600

    (decision,) = sched.plan([_job(1)], {}, {"host-a"}, review_sessions=0, ram_offers={})

    assert isinstance(decision, AdmitDecision)


def test_a_hold_never_removes_a_running_lane(config) -> None:
    sched = LocalScheduler(config, Ledger(), clock=Clock())
    admit = sched.plan([_job(1, "heavy")], {}, {"host-a"}, review_sessions=0, ram_offers={})[0]
    assert isinstance(admit, AdmitDecision)
    sched.commit(admit, lane_id="lane-1", container_id="c1", idle_since=0.0)

    sched.put_hold("release", host="host-a", ram_mb=11000, ttl_seconds=600)
    sched.plan([_job(2)], {}, {"host-a"}, review_sessions=0, ram_offers={})

    assert [r.lane_id for r in sched.lanes()] == ["lane-1"]


# --- HTTP API -----------------------------------------------------------------------------


@pytest.fixture()
def api(config):
    clock = Clock()
    sched = LocalScheduler(config, Ledger(), clock=clock)
    return TestClient(create_scheduler_app(sched)), sched, clock


def test_put_then_get_reports_the_hold_and_every_hosts_ram(api) -> None:
    client, sched, clock = api
    lane = sched.plan([_job(1, "heavy")], {}, {"host-a"}, review_sessions=0, ram_offers={})[0]
    assert isinstance(lane, AdmitDecision)
    sched.commit(lane, lane_id="lane-1", container_id="c1", idle_since=0.0)

    r = client.put("/holds/release", json={"host": "host-a", "ram_mb": 9216, "ttl_seconds": 7200})
    assert r.status_code == 200
    assert r.json() == {
        "name": "release",
        "host": "host-a",
        "ram_mb": 9216,
        "expires_at": clock.now + 7200,
        "remaining_seconds": 7200,
    }

    clock.now += 200
    assert client.get("/holds").json() == {
        "holds": [
            {
                "name": "release",
                "host": "host-a",
                "ram_mb": 9216,
                "expires_at": clock.now - 200 + 7200,
                "remaining_seconds": 7000,
            }
        ],
        "hosts": {
            "host-a": {"budget_ram_mb": 11000, "lanes_ram_mb": 4000, "held_ram_mb": 9216},
            "host-b": {"budget_ram_mb": 6000, "lanes_ram_mb": 0, "held_ram_mb": 0},
        },
    }


def test_an_expired_hold_is_not_returned(api) -> None:
    client, _sched, clock = api
    client.put("/holds/release", json={"host": "host-a", "ram_mb": 1000, "ttl_seconds": 60})
    clock.now += 60

    body = client.get("/holds").json()

    assert body["holds"] == []
    assert body["hosts"]["host-a"]["held_ram_mb"] == 0


def test_delete_is_idempotent(api) -> None:
    client, _sched, _clock = api
    client.put("/holds/release", json={"host": "host-a", "ram_mb": 1000, "ttl_seconds": 600})

    assert client.delete("/holds/release").status_code == 204
    assert client.delete("/holds/release").status_code == 204
    assert client.get("/holds").json()["holds"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"host": "nowhere", "ram_mb": 1000, "ttl_seconds": 600},
        {"ram_mb": 1000, "ttl_seconds": 600},
        {"host": "host-a", "ram_mb": 0, "ttl_seconds": 600},
        {"host": "host-a", "ram_mb": 1000, "ttl_seconds": 59},
        {"host": "host-a", "ram_mb": 1000, "ttl_seconds": 43201},
        {"host": "host-b", "ram_mb": 6001, "ttl_seconds": 600},
    ],
    ids=["unknown-host", "no-host", "zero-ram", "ttl-short", "ttl-long", "over-ceiling"],
)
def test_put_refuses_an_invalid_hold(api, body) -> None:
    client, _sched, _clock = api

    r = client.put("/holds/release", json=body)

    assert r.status_code == 422
    assert client.get("/holds").json()["holds"] == []


def test_put_refuses_a_name_that_is_not_one_plain_token(api) -> None:
    client, _sched, _clock = api

    r = client.put("/holds/two words", json={"host": "host-a", "ram_mb": 1, "ttl_seconds": 600})

    assert r.status_code == 422


# --- parity -------------------------------------------------------------------------------


def test_local_and_http_schedulers_report_the_same_holds(config) -> None:
    clock = Clock()
    local = LocalScheduler(config, Ledger(), clock=clock)
    backing = LocalScheduler(config, Ledger(), clock=clock)
    remote = HttpScheduler(
        "http://sched",
        transport=_TestClientTransport(TestClient(create_scheduler_app(backing))),
        retries=1,
        backoff=0.0,
    )
    local.put_hold("release", host="host-a", ram_mb=9000, ttl_seconds=600)
    backing.put_hold("release", host="host-a", ram_mb=9000, ttl_seconds=600)

    assert local.holds() == remote.holds()
    assert remote.holds() == [
        Hold(name="release", host="host-a", ram_mb=9000, expires_at=clock.now + 600)
    ]

    local_plan = local.plan([_job(1, "heavy")], {}, None, review_sessions=0, ram_offers={})
    remote_plan = remote.plan([_job(1, "heavy")], {}, None, review_sessions=0, ram_offers={})
    assert [getattr(d, "host", None) for d in local_plan] == ["host-b"]
    assert [getattr(d, "host", None) for d in remote_plan] == ["host-b"]


# --- the dispatcher's view ------------------------------------------------------------------


def test_status_reports_each_hosts_held_ram_and_every_hold(write_config, monkeypatch) -> None:
    fleet = Fleet(write_config, monkeypatch)
    fleet.scheduler.put_hold("release", host="host-a", ram_mb=9216, ttl_seconds=7200)
    fleet.now += 200
    fleet.ctrl.tick()

    status = fleet.ctrl.status()

    assert status["hosts"]["host-a"]["held_ram_mb"] == 9216
    assert status["hosts"]["host-b"]["held_ram_mb"] == 0
    assert status["holds"] == [
        {
            "name": "release",
            "host": "host-a",
            "ram_mb": 9216,
            "expires_at": fleet.now - 200 + 7200,
            "remaining_seconds": 7000,
        }
    ]


def test_a_hold_sends_the_dispatchers_next_lane_to_another_host(write_config, monkeypatch) -> None:
    fleet = Fleet(write_config, monkeypatch)
    fleet.scheduler.put_hold("release", host="host-a", ram_mb=10500, ttl_seconds=600)
    fleet.queue(1)

    fleet.ctrl.tick()

    fleet.adapters["host-a"].spawn.assert_not_called()
    fleet.adapters["host-b"].spawn.assert_called_once()


def test_a_scheduler_that_predates_holds_reports_them_unknown_not_empty(
    write_config, monkeypatch
) -> None:
    """Another operator's scheduler may run an older pin. /status is the dispatcher's
    healthcheck, so it must keep answering -- and say "unknown", never "none held"."""
    fleet = Fleet(write_config, monkeypatch)

    def no_route() -> list[Hold]:
        raise KeyError("/holds")

    monkeypatch.setattr(fleet.scheduler, "holds", no_route)

    status = fleet.ctrl.status()

    assert status["holds"] is None
    assert status["hosts"]["host-a"]["held_ram_mb"] is None
