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

    def _matching(self, host: str | None) -> list[Reservation]:
        items = self._items.values()
        if host is None:
            return list(items)
        return [r for r in items if r.host == host]

    def total_ram(self, host: str | None = None) -> int:
        return sum(r.ram_mb for r in self._matching(host))

    def lane_count(self, host: str | None = None) -> int:
        return len(self._matching(host))

    def kvm_in_use(self, host: str | None = None) -> bool:
        return any(r.needs_kvm for r in self._matching(host))

    def disk_gb_in_use(self, disk: str, host: str | None = None) -> int:
        return sum(r.work_gb for r in self._matching(host) if r.work_disk == disk)

    def has_job(self, job_id: int) -> bool:
        # Deliberately global, no host filter: the ledger is the single admission
        # gate for a GitHub job_id. A host-scoped lookup here would let the same
        # job be admitted on two hosts at once, leaving an orphan ephemeral runner
        # on whichever host loses the race.
        return any(r.job_id == job_id for r in self._items.values())

    def lane_ids(self, host: str | None = None) -> set[str]:
        return {r.lane_id for r in self._matching(host)}

    def reservations(self, host: str | None = None) -> list[Reservation]:
        return self._matching(host)
