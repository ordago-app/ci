from __future__ import annotations

from src.models import Reservation


class Ledger:
    """In-memory accounting of currently-admitted runner lanes (single source of truth)."""

    def __init__(self) -> None:
        self._items: dict[str, Reservation] = {}

    def add(self, reservation: Reservation) -> None:
        if reservation.lane_id in self._items:
            raise ValueError(f"lane '{reservation.lane_id}' already in ledger")
        self._items[reservation.lane_id] = reservation

    def remove(self, lane_id: str) -> None:
        self._items.pop(lane_id, None)

    def total_ram(self) -> int:
        return sum(r.ram_mb for r in self._items.values())

    def lane_count(self) -> int:
        return len(self._items)

    def kvm_in_use(self) -> bool:
        return any(r.needs_kvm for r in self._items.values())

    def disk_gb_in_use(self, disk: str) -> int:
        return sum(r.work_gb for r in self._items.values() if r.work_disk == disk)

    def has_job(self, job_id: int) -> bool:
        return any(r.job_id == job_id for r in self._items.values())

    def lane_ids(self) -> set[str]:
        return set(self._items)

    def reservations(self) -> list[Reservation]:
        return list(self._items.values())
