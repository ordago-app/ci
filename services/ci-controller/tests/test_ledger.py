import pytest
from src.ledger import Ledger
from src.models import Reservation


def _res(lane_id: str, job_id: int, ram_mb: int = 700, kvm: bool = False) -> Reservation:
    return Reservation(
        lane_id=lane_id,
        job_id=job_id,
        repo="o/r",
        class_name="light",
        ram_mb=ram_mb,
        needs_kvm=kvm,
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


def test_add_duplicate_lane_id_rejected() -> None:
    led = Ledger()
    led.add(_res("a", 1))
    with pytest.raises(ValueError, match="already"):
        led.add(_res("a", 2))
