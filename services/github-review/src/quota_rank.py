"""Order a chain's Claude subscriptions by the weekly credit each would lose unused.

A subscription's weekly allowance expires at its reset whether or not it was
spent, so with several subscriptions the cheapest review is the one taken from
the account that would otherwise waste the most. The fixed `providers` chain
can't know that; this module reorders the subscriptions in it, and only them.
Codex and API-key accounts keep their slots.

Where the numbers come from. A `claude setup-token` token cannot read
`/api/oauth/usage` (403 `oauth_scope_insufficient`, it lacks `user:profile`;
tested 2026-11-20). Every Messages response carries the account's
`anthropic-ratelimit-unified-*` headers instead, so `QuotaProber` sends a
one-token request per account at fixed times of day, and the review runs add
whatever their `rate_limit_event` says in between. A request opens the account's
5-hour window, so the probe times are meant to be the times the operator wants
windows to open anyway; a free-running poll would open windows at random.

What the ranking may not do is fail closed. An account with no reading keeps
its configured place behind the ranked ones, and a full one moves last rather
than out: failover still reaches it, because readings are hours old and a
"full" account may have reset since.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import httpx

from .config import ClaudeAccount, QuotaRanking

_log = logging.getLogger(__name__)

WindowKind = Literal["five_hour", "seven_day"]

# An account above either share is treated as unable to take a review now.
WEEKLY_FULL = 0.95
FIVE_HOUR_FULL = 0.85
# A 5-hour reading describes minutes, not hours: older than this it says nothing
# about whether the window has room now, and failover covers the gap.
FIVE_HOUR_TRUSTED_SECONDS = 15 * 60
# Probes run a few times a day; weekly use only grows between them, so a reading
# up to a day old still ranks, and an older one is no reading.
WEEKLY_TRUSTED_SECONDS = 24 * 3600
# One 5-hour window spends only part of a week, so credit due to expire in an
# hour cannot all be used in that hour. Counting at least a window's length stops
# an account near its reset from outranking everything on a tiny denominator.
MIN_HOURS_TO_SPEND = 5.0

PROBE_URL = "https://api.anthropic.com/v1/messages"
# Subscription OAuth tokens are accepted on the Messages API only as Claude Code.
_PROBE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
_PROBE_ATTEMPTS = 3
_PROBE_BACKOFF_SECONDS = (5.0, 15.0)


@dataclass(frozen=True)
class Window:
    utilization: float
    resets_at: float
    observed_at: float

    def as_dict(self) -> dict[str, float]:
        return {
            "utilization": self.utilization,
            "resets_at": self.resets_at,
            "observed_at": self.observed_at,
        }


class QuotaLedger:
    """The latest reading of each account's 5-hour and weekly window.

    In memory on purpose, like the worker's outages: after a restart the ranking
    falls back to the configured order until the next probe, rather than ranking
    on numbers of unknown age."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, dict[WindowKind, Window]] = {}

    def record(self, account: str, kind: WindowKind, window: Window) -> None:
        with self._lock:
            self._windows.setdefault(account, {})[kind] = window

    def windows(self, account: str) -> dict[WindowKind, Window]:
        with self._lock:
            return dict(self._windows.get(account, {}))


def windows_from_headers(headers: Mapping[str, str], now: float) -> dict[WindowKind, Window]:
    """The windows an `anthropic-ratelimit-unified-*` header set describes.

    A window missing either its utilization or its reset is left out, not
    zero-filled: a missing number is not an empty account."""
    lowered = {k.lower(): v for k, v in headers.items()}
    found: dict[WindowKind, Window] = {}
    prefixes: tuple[tuple[WindowKind, str], ...] = (("five_hour", "5h"), ("seven_day", "7d"))
    for kind, prefix in prefixes:
        raw_util = lowered.get(f"anthropic-ratelimit-unified-{prefix}-utilization")
        raw_reset = lowered.get(f"anthropic-ratelimit-unified-{prefix}-reset")
        try:
            utilization, resets_at = float(raw_util or ""), float(raw_reset or "")
        except ValueError:
            continue
        found[kind] = Window(utilization=utilization, resets_at=resets_at, observed_at=now)
    return found


def window_kind(limit_type: str) -> WindowKind | None:
    """The window a CLI `rate_limit_event`'s `rateLimitType` reports on, if one we rank by."""
    if limit_type == "five_hour":
        return "five_hour"
    if limit_type in ("seven_day", "weekly"):
        return "seven_day"
    return None


def expiring_per_hour(weekly: Window, now: float) -> float:
    """Share of a week's allowance that would expire unused, per hour left to spend it."""
    hours = max((weekly.resets_at - now) / 3600, MIN_HOURS_TO_SPEND)
    return (1.0 - weekly.utilization) / hours


Standing = Literal["usable", "unknown", "full"]


def _standing(windows: Mapping[WindowKind, Window], now: float) -> Standing:
    weekly = windows.get("seven_day")
    stale = weekly is not None and now - weekly.observed_at > WEEKLY_TRUSTED_SECONDS
    if weekly is None or weekly.resets_at <= now or stale:
        return "unknown"
    if weekly.utilization >= WEEKLY_FULL:
        return "full"
    five = windows.get("five_hour")
    if (
        five is not None
        and five.resets_at > now
        and now - five.observed_at <= FIVE_HOUR_TRUSTED_SECONDS
        and five.utilization >= FIVE_HOUR_FULL
    ):
        return "full"
    return "usable"


def rank(accounts: Sequence[str], ledger: QuotaLedger, now: float) -> list[str]:
    """Usable accounts by credit expiring soonest, then unread ones, then full ones.

    Ties and the unread and full groups keep the order they were given in, so an
    instance with no readings at all reviews exactly as its config says."""
    usable: list[tuple[float, str]] = []
    unknown: list[str] = []
    full: list[str] = []
    for name in accounts:
        windows = ledger.windows(name)
        standing = _standing(windows, now)
        if standing == "usable":
            usable.append((expiring_per_hour(windows["seven_day"], now), name))
        elif standing == "unknown":
            unknown.append(name)
        else:
            full.append(name)
    usable.sort(key=lambda pair: pair[0], reverse=True)
    return [name for _, name in usable] + unknown + full


def order_chain(
    chain: Sequence[str], ranked: Sequence[str], ledger: QuotaLedger, now: float
) -> list[str]:
    """`chain` with the slots its ranked accounts hold refilled in ranked order.

    Every other entry stays where the config put it: `[codex, api, sub-a, sub-b]`
    can become `[codex, api, sub-b, sub-a]`, never `[sub-b, codex, ...]`."""
    members = [name for name in chain if name in ranked]
    reordered = iter(rank(members, ledger, now))
    return [next(reordered) if name in ranked else name for name in chain]


class QuotaProber:
    """Reads each ranked account's windows with a one-token Messages request."""

    def __init__(
        self,
        *,
        accounts: Mapping[str, ClaudeAccount],
        ledger: QuotaLedger,
        model: str,
        environ: Mapping[str, str],
        http: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> None:
        self._accounts = accounts
        self._ledger = ledger
        self._model = model
        self._environ = environ
        self._http = http or httpx.Client(timeout=30)
        self._sleep = sleep

    def probe_all(self) -> None:
        """Probe every account. Never raises: a missed reading costs ranking, not reviews."""
        for name in self._accounts:
            try:
                self._probe(name)
            except Exception:
                _log.exception("quota probe of %s failed", name)

    def _probe(self, name: str) -> None:
        token = self._accounts[name].credential.read(self._environ)
        if not token:
            _log.warning("quota probe skipped %s: no credential", name)
            return
        response = self._post(token)
        windows = windows_from_headers(response.headers, time.time())
        if not windows:
            _log.warning(
                "quota probe of %s: HTTP %s carried no unified rate-limit headers",
                name,
                response.status_code,
            )
            return
        for kind, window in windows.items():
            self._ledger.record(name, kind, window)
        _log.info(
            "quota %s: %s",
            name,
            ", ".join(
                f"{kind} {w.utilization:.0%} until "
                f"{datetime.fromtimestamp(w.resets_at).isoformat(timespec='minutes')}"
                for kind, w in windows.items()
            ),
        )

    def _post(self, token: str) -> httpx.Response:
        """One request, retried only on transport errors and 5xx.

        A 4xx is an answer: a 429 still carries the headers that say the
        account is full, and a 401 will not improve on retry."""
        for attempt in range(_PROBE_ATTEMPTS):
            try:
                response = self._http.post(
                    PROBE_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "anthropic-beta": "oauth-2025-04-20",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": self._model,
                        "max_tokens": 1,
                        "system": _PROBE_SYSTEM,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
            except httpx.TransportError:
                if attempt == _PROBE_ATTEMPTS - 1:
                    raise
            else:
                if response.status_code < 500 or attempt == _PROBE_ATTEMPTS - 1:
                    return response
            self._sleep(_PROBE_BACKOFF_SECONDS[min(attempt, len(_PROBE_BACKOFF_SECONDS) - 1)])
        raise AssertionError("unreachable")


def next_probe(settings: QuotaRanking, after: datetime) -> datetime:
    """The first configured local time strictly after `after` (timezone-aware)."""
    zone = ZoneInfo(settings.timezone)
    local = after.astimezone(zone)
    for day in range(2):
        date = (local + timedelta(days=day)).date()
        for hhmm in sorted(settings.probe_at):
            hour, minute = (int(part) for part in hhmm.split(":"))
            candidate = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone)
            if candidate > local:
                return candidate
    raise AssertionError("probe_at is never empty")


def run_probes(settings: QuotaRanking, prober: QuotaProber, stop: threading.Event) -> None:
    """Probe at each configured time until `stop` is set. Run on its own thread."""
    while True:
        now = datetime.now().astimezone()
        due = next_probe(settings, now)
        if stop.wait(timeout=(due - now).total_seconds()):
            return
        prober.probe_all()
