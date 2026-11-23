from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

# A hold shorter than a minute expires before the lanes it waits on can drain; one longer
# than twelve hours is a reservation somebody forgot, starving CI on that host overnight.
HOLD_TTL_MIN_SECONDS = 60
HOLD_TTL_MAX_SECONDS = 43200


@dataclass(frozen=True)
class Hold:
    """RAM set aside on one host for work the pool does not schedule -- an operator's
    build or emulator run on a machine that also runs lanes. Admission charges it against
    the host's budget exactly like a lane, so new lanes stop landing there; it never
    evicts a lane already running."""

    name: str
    host: str
    ram_mb: int
    expires_at: float

    def remaining_seconds(self, now: float) -> int:
        return max(0, int(self.expires_at - now))


class HoldBook:
    """Named holds with a TTL, kept in memory beside the ledger and lost with it.

    A scheduler restart drops every hold. That is deliberate rather than a gap: a hold
    is renewed by its owner re-reserving under the same name (`hold_cli reserve`), and
    one that outlived a crashed owner should not outlive the scheduler too. A caller
    that needs its hold for a long run renews it inside its TTL."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._holds: dict[str, Hold] = {}

    def put(self, name: str, *, host: str, ram_mb: int, ttl_seconds: int) -> Hold:
        """Create or replace `name`. Re-putting renews the TTL and may move or resize it."""
        if ram_mb <= 0:
            raise ValueError("ram_mb must be positive")
        if not HOLD_TTL_MIN_SECONDS <= ttl_seconds <= HOLD_TTL_MAX_SECONDS:
            raise ValueError(
                f"ttl_seconds must be within {HOLD_TTL_MIN_SECONDS}..{HOLD_TTL_MAX_SECONDS}"
            )
        hold = Hold(name=name, host=host, ram_mb=ram_mb, expires_at=self._clock() + ttl_seconds)
        self._holds[name] = hold
        return hold

    def remove(self, name: str) -> None:
        self._holds.pop(name, None)

    def active(self) -> list[Hold]:
        """Unexpired holds, by name. Expired ones are pruned here, so no reader can see
        or charge one."""
        now = self._clock()
        self._holds = {name: h for name, h in self._holds.items() if h.expires_at > now}
        return [self._holds[name] for name in sorted(self._holds)]

    def held_ram_mb(self) -> dict[str, int]:
        held: dict[str, int] = defaultdict(int)
        for hold in self.active():
            held[hold.host] += hold.ram_mb
        return dict(held)

    def now(self) -> float:
        return self._clock()
