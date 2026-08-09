import pytest
from src.ledger import Ledger
from src.models import Reservation


def _res(
    lane_id: str,
    job_id: int,
    ram_mb: int = 700,
    kvm: bool = False,
    work_disk: str = "ssd",
    work_gb: int = 0,
) -> Reservation:
    return Reservation(
        lane_id=lane_id,
        spawned_for_job_id=job_id,
        repo="o/r",
        class_name="light",
        ram_mb=ram_mb,
        needs_kvm=kvm,
        work_disk=work_disk,
        work_gb=work_gb,
    )


def test_add_tracks_totals() -> None:
    led = Ledger()
    led.add(_res("a", 1, ram_mb=700))
    led.add(_res("b", 2, ram_mb=1500))
    assert led.total_ram() == 2200
    assert led.lane_count() == 2


def test_remove_frees_budget() -> None:
    led = Ledger()
    led.add(_res("a", 1, ram_mb=700))
    led.add(_res("b", 2, ram_mb=1500))
    led.remove("a")
    assert led.total_ram() == 1500
    assert led.lane_count() == 1


def test_remove_unknown_lane_is_noop() -> None:
    led = Ledger()
    led.remove("ghost")  # must not raise
    assert led.lane_count() == 0


def test_kvm_in_use() -> None:
    led = Ledger()
    assert led.kvm_in_use() is False
    led.add(_res("a", 1, kvm=True))
    assert led.kvm_in_use() is True
    led.remove("a")
    assert led.kvm_in_use() is False


def test_has_job() -> None:
    led = Ledger()
    led.add(_res("a", 42))
    assert led.has_job(42) is True
    assert led.has_job(99) is False


def test_disk_gb_in_use_is_per_disk() -> None:
    led = Ledger()
    led.add(_res("a", 1, work_disk="ssd", work_gb=4))
    led.add(_res("b", 2, work_disk="ssd", work_gb=6))
    led.add(_res("c", 3, work_disk="hdd", work_gb=50))
    assert led.disk_gb_in_use("ssd") == 10
    assert led.disk_gb_in_use("hdd") == 50
    led.remove("b")
    assert led.disk_gb_in_use("ssd") == 4


def test_add_duplicate_lane_id_rejected() -> None:
    led = Ledger()
    led.add(_res("a", 1))
    with pytest.raises(ValueError, match="already"):
        led.add(_res("a", 2))


def _res2(**over) -> Reservation:
    base = dict(
        lane_id="powerserver-cici-1",
        spawned_for_job_id=1,
        repo="alvaro-francisco-gil/homelab",
        class_name="light",
        ram_mb=700,
        needs_kvm=False,
    )
    base.update(over)
    return Reservation(**base)


def test_claimed_job_is_the_spawned_for_job_until_attributed() -> None:
    assert _res2().claimed_job_id == 1


def test_claimed_job_is_the_running_job_once_attributed() -> None:
    assert _res2(running_job_id=2).claimed_job_id == 2


def test_attribution_releases_the_claim_on_the_spawned_for_job() -> None:
    """The starvation fix: job 1 was stolen by job 2, so 1 must become admissible again."""
    ledger = Ledger()
    ledger.add(_res2())
    assert ledger.has_job(1)

    ledger.update("powerserver-cici-1", running_job_id=2)

    assert not ledger.has_job(1)
    assert ledger.has_job(2)


def test_update_preserves_untouched_fields() -> None:
    ledger = Ledger()
    ledger.add(_res2(ram_mb=5000, class_name="node"))
    ledger.update("powerserver-cici-1", runner_id=77)
    (res,) = ledger.reservations()
    assert (res.runner_id, res.ram_mb, res.class_name) == (77, 5000, "node")


def test_update_rejects_an_unknown_lane() -> None:
    with pytest.raises(KeyError):
        Ledger().update("nope", runner_id=1)


def test_per_host_totals_are_separate_but_has_job_is_global() -> None:
    led = Ledger()
    led.add(
        Reservation(
            lane_id="a",
            spawned_for_job_id=1,
            repo="r",
            class_name="light",
            ram_mb=700,
            needs_kvm=False,
            host="powerserver",
        )
    )
    led.add(
        Reservation(
            lane_id="b",
            spawned_for_job_id=2,
            repo="r",
            class_name="light",
            ram_mb=700,
            needs_kvm=False,
            host="powervaro-ci",
        )
    )
    assert led.total_ram() == 1400
    assert led.total_ram(host="powerserver") == 700
    assert led.lane_count(host="powervaro-ci") == 1
    assert led.has_job(1) and led.has_job(2)  # global: no host filter exists
