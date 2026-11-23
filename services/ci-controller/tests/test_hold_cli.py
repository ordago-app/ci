from __future__ import annotations

import io

import httpx
import pytest
from fastapi.testclient import TestClient
from src import hold_cli
from src.config import ControllerConfig
from src.ledger import Ledger
from src.models import Reservation
from src.scheduler import LocalScheduler
from src.scheduler_api import create_scheduler_app

from tests.test_scheduler_parity import _TestClientTransport
from tests.two_host_fleet import TWO_HOST_CONFIG, Clock


def _lane(lane_id: str, ram_mb: int, host: str = "host-a") -> Reservation:
    return Reservation(
        lane_id=lane_id,
        spawned_for_job_id=1,
        repo="ordago-app/ordago-apps",
        class_name="heavy",
        ram_mb=ram_mb,
        needs_kvm=False,
        work_disk="ssd",
        host=host,
    )


class _Run:
    """The CLI against a real scheduler app, on a fake clock that `sleep` advances.
    `on_sleep` lets a test drain lanes while the CLI waits."""

    def __init__(self, write_config) -> None:
        self.clock = Clock(0.0)
        self.ledger = Ledger()
        config = ControllerConfig.load(write_config(TWO_HOST_CONFIG))
        self.scheduler = LocalScheduler(config, self.ledger, clock=self.clock)
        self.transport = _TestClientTransport(TestClient(create_scheduler_app(self.scheduler)))
        self.sleeps: list[float] = []
        self.on_sleep = lambda: None

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.now += seconds
        self.on_sleep()

    def __call__(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = hold_cli.main(
            list(argv),
            transport=self.transport,
            sleep=self._sleep,
            clock=self.clock,
            out=out,
            err=err,
        )
        return code, out.getvalue(), err.getvalue()


@pytest.fixture()
def run(write_config) -> _Run:
    return _Run(write_config)


def _reserve(*extra: str) -> list[str]:
    return ["reserve", "--name", "release", "--host", "host-a", "--ttl", "2h", *extra]


def test_reserve_on_an_idle_host_fits_at_once(run) -> None:
    code, out, _ = run(*_reserve("--gb", "9"))

    assert code == hold_cli.EXIT_OK
    assert out.splitlines() == [
        "Hold release: 9216 MB on host-a for 2h00m. New lanes on host-a now count it; "
        "lanes already running there finish normally.",
        "Fits: host-a: lanes 0 MB + held 9216 MB of 11000 MB budget. "
        "Renew before it expires by running reserve again.",
    ]
    assert run.sleeps == []


def test_reserve_waits_for_lanes_to_drain_and_reports_each_change(run) -> None:
    run.ledger.add(_lane("l1", 4000))
    run.ledger.add(_lane("l2", 4000))
    drained = iter(["l1", None, "l2"])

    def drain() -> None:
        lane = next(drained, None)
        if lane is not None:
            run.ledger.remove(lane)

    run.on_sleep = drain

    code, out, _ = run(*_reserve("--mb", "4000", "--wait", "15m"))

    assert code == hold_cli.EXIT_OK
    assert out.splitlines()[1:] == [
        "Waiting for lanes to finish: host-a: lanes 8000 MB + held 4000 MB of 11000 MB budget.",
        "Fits: host-a: lanes 4000 MB + held 4000 MB of 11000 MB budget. "
        "Renew before it expires by running reserve again.",
    ]
    assert run.sleeps == [hold_cli.POLL_SECONDS]


def test_reserve_prints_one_line_per_change_not_per_poll(run) -> None:
    run.ledger.add(_lane("l1", 4000))
    run.ledger.add(_lane("l2", 4000))
    polls = iter([None, None, "l1", None, "l2"])

    def drain() -> None:
        lane = next(polls, None)
        if lane is not None:
            run.ledger.remove(lane)

    run.on_sleep = drain

    code, out, _ = run(*_reserve("--gb", "8"))

    assert code == hold_cli.EXIT_OK
    waiting = [line for line in out.splitlines() if line.startswith("Waiting")]
    assert waiting == [
        "Waiting for lanes to finish: host-a: lanes 8000 MB + held 8192 MB of 11000 MB budget.",
        "Waiting for lanes to finish: host-a: lanes 4000 MB + held 8192 MB of 11000 MB budget.",
    ]


def test_a_wait_that_runs_out_exits_75_and_says_the_hold_is_in_place(run) -> None:
    run.ledger.add(_lane("l1", 8000))

    code, out, _ = run(*_reserve("--gb", "9", "--wait", "90s"))

    assert code == hold_cli.EXIT_STILL_DRAINING == 75
    assert out.splitlines()[-1] == (
        "Gave up waiting after 1m30s. The hold IS in place and no new lanes start on "
        "host-a, but running lanes are still draining (6216 MB over budget). Reserve "
        "again to keep waiting, or release it."
    )
    assert sum(run.sleeps) == 90
    assert all(s <= hold_cli.POLL_SECONDS for s in run.sleeps)
    assert [h.name for h in run.scheduler.holds()] == ["release"]


def test_wait_zero_places_the_hold_and_returns_without_waiting(run) -> None:
    run.ledger.add(_lane("l1", 8000))

    code, out, _ = run(*_reserve("--gb", "9", "--wait", "0"))

    assert code == hold_cli.EXIT_OK
    assert out.splitlines()[-1] == (
        "Not waiting: host-a: lanes 8000 MB + held 9216 MB of 11000 MB budget. "
        "Lanes there drain as they finish."
    )
    assert run.sleeps == []
    assert [h.name for h in run.scheduler.holds()] == ["release"]


def test_reserving_again_under_the_same_name_renews(run) -> None:
    run(*_reserve("--gb", "9"))
    run.clock.now += 3600

    run(*_reserve("--gb", "9"))

    (hold,) = run.scheduler.holds()
    assert hold.remaining_seconds(run.clock.now) == 7200


def test_a_hold_lost_to_a_scheduler_restart_is_placed_again_while_waiting(run) -> None:
    run.ledger.add(_lane("l1", 8000))

    def restart_then_drain() -> None:
        run.scheduler.release_hold("release")
        run.ledger.remove("l1")

    run.on_sleep = restart_then_drain

    code, out, _ = run(*_reserve("--gb", "9"))

    assert code == hold_cli.EXIT_OK
    assert "Hold release is gone (the scheduler restarted?); placing it again." in out
    assert [h.name for h in run.scheduler.holds()] == ["release"]


def test_release_drops_the_hold(run) -> None:
    run(*_reserve("--gb", "1"))

    code, out, _ = run("release", "--name", "release")

    assert code == hold_cli.EXIT_OK
    assert out == "Released hold release.\n"
    assert run.scheduler.holds() == []


def test_list_shows_every_hold_with_its_remaining_time_and_each_hosts_ram(run) -> None:
    run.ledger.add(_lane("l1", 700, host="host-b"))
    run(*_reserve("--gb", "9"))
    run("reserve", "--name", "emu", "--host", "host-b", "--mb", "512", "--ttl", "90m")
    run.clock.now += 61

    code, out, _ = run("list")

    assert code == hold_cli.EXIT_OK
    assert out.splitlines() == [
        "NAME     HOST    RAM_MB  REMAINING",
        "emu      host-b  512     1h28m",
        "release  host-a  9216    1h58m",
        "",
        "host-a: lanes 0 MB + held 9216 MB of 11000 MB budget",
        "host-b: lanes 700 MB + held 512 MB of 6000 MB budget",
    ]


def test_list_says_when_there_are_no_holds(run) -> None:
    _code, out, _ = run("list")

    assert out.splitlines()[0] == "No holds."


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--host", "nowhere", "--gb", "1", "--ttl", "2h"], "unknown host 'nowhere'"),
        (["--host", "host-a", "--gb", "1", "--ttl", "30s"], "ttl_seconds"),
        (["--host", "host-b", "--gb", "7", "--ttl", "2h"], "exceeds host-b's whole"),
    ],
    ids=["unknown-host", "ttl-too-short", "over-ceiling"],
)
def test_a_refused_hold_exits_2_with_the_schedulers_reason(run, argv, message) -> None:
    code, _, err = run("reserve", "--name", "release", *argv)

    assert code == hold_cli.EXIT_REFUSED
    assert message in err
    assert run.scheduler.holds() == []


def test_an_unreachable_scheduler_is_retried_a_bounded_number_of_times(write_config) -> None:
    attempts: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        raise httpx.ConnectError("connection refused", request=request)

    sleeps: list[float] = []
    err = io.StringIO()
    code = hold_cli.main(
        ["list"],
        transport=httpx.MockTransport(refuse),
        sleep=sleeps.append,
        clock=lambda: 0.0,
        out=io.StringIO(),
        err=err,
    )

    assert code == hold_cli.EXIT_UNAVAILABLE == 69
    assert attempts == ["/holds"] * 3
    assert sleeps == [1.0, 2.0]
    assert "did not answer GET /holds after 3 attempts" in err.getvalue()


def test_a_5xx_is_retried_and_a_later_answer_is_used(write_config) -> None:
    answers = iter([httpx.Response(503), httpx.Response(200, json={"holds": [], "hosts": {}})])
    out = io.StringIO()

    code = hold_cli.main(
        ["list"],
        transport=httpx.MockTransport(lambda request: next(answers)),
        sleep=lambda _s: None,
        clock=lambda: 0.0,
        out=out,
        err=io.StringIO(),
    )

    assert code == hold_cli.EXIT_OK
    assert out.getvalue().startswith("No holds.")


@pytest.mark.parametrize(
    ("text", "seconds"), [("2h", 7200), ("90m", 5400), ("3600s", 3600), ("0", 0)]
)
def test_durations(text: str, seconds: int) -> None:
    assert hold_cli.parse_duration(text) == seconds


def test_gb_and_mb_are_mutually_exclusive(run) -> None:
    with pytest.raises(SystemExit) as exc:
        run(*_reserve("--gb", "1", "--mb", "1024"))
    assert exc.value.code == 2
