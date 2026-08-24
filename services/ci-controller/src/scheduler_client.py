from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Any

import httpx

from src.host_stats import HostStats
from src.models import AdmitDecision, Decision, QueuedJob, Reservation
from src.scheduler_models import decision_from_wire, decision_to_wire, reservation_from_wire

log = logging.getLogger("ci-controller")


class SchedulerUnavailable(RuntimeError):
    """The scheduler could not be reached after the configured retries.

    Raised rather than returning an empty plan: an empty plan is indistinguishable
    from "nothing to admit", and would silently stall CI while looking healthy."""


class HttpScheduler:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        retries: int = 3,
        backoff: float = 0.5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self._retries = retries
        self._backoff = backoff

    def _request(self, method: str, path: str, *, retry: bool, **kwargs: Any) -> httpx.Response:
        """`retry` is per-call rather than derived from the verb: a retried write can be
        applied twice, and `POST /lanes` in particular answers a duplicate `lane_id` with a
        500 the caller would read as "the lane was never reserved"."""
        attempts = self._retries if retry else 1
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._client.request(method, path, **kwargs)
                if response.status_code == 404:
                    raise KeyError(path)
                response.raise_for_status()
                return response
            except KeyError:
                raise
            except httpx.HTTPError as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    raise
                last = exc
            if attempt + 1 < attempts:
                time.sleep(self._backoff * (2**attempt))
        log.warning("%s %s failed after %d attempts: %s", method, path, attempts, last)
        raise SchedulerUnavailable(f"{method} {path} failed after {attempts} attempts: {last}")

    def plan(
        self,
        jobs: list[QueuedJob],
        host_stats: dict[str, HostStats],
        healthy: set[str] | None,
    ) -> list[Decision]:
        body = {
            "jobs": [asdict(j) for j in jobs],
            "host_stats": {name: asdict(s) for name, s in host_stats.items()},
            "healthy": sorted(healthy) if healthy is not None else None,
        }
        # A plan is a pure computation over state the scheduler already holds, so it is
        # safe to retry despite the verb.
        data = self._request("POST", "/plan", json=body, retry=True).json()
        return [decision_from_wire(d) for d in data["decisions"]]

    def commit(
        self,
        decision: AdmitDecision,
        *,
        lane_id: str,
        container_id: str,
        idle_since: float,
    ) -> None:
        self._request(
            "POST",
            "/lanes",
            retry=False,
            json={
                "decision": decision_to_wire(decision),
                "lane_id": lane_id,
                "container_id": container_id,
                "idle_since": idle_since,
            },
        )

    def adopt(self, reservation: Reservation) -> None:
        self._request(
            "POST", "/lanes/adopt", retry=False, json={"reservation": asdict(reservation)}
        )

    def update(self, lane_id: str, **fields: object) -> None:
        self._request("PATCH", f"/lanes/{lane_id}", retry=True, json={"fields": fields})

    def release(self, lane_id: str) -> None:
        self._request("DELETE", f"/lanes/{lane_id}", retry=True)

    def lanes(self) -> list[Reservation]:
        data = self._request("GET", "/lanes", retry=True).json()
        return [reservation_from_wire(r) for r in data["lanes"]]
