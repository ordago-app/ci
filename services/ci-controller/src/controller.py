from __future__ import annotations

import logging
import time
from collections.abc import Callable

from src.admission import evaluate
from src.config import ControllerConfig
from src.docker_adapter import DockerAdapter
from src.github_adapter import GitHubAdapter
from src.host_stats import HostStats
from src.ledger import Ledger
from src.metrics import MetricsStore
from src.models import AdmitDecision, Decision, DeferDecision, QueuedJob, Reservation

log = logging.getLogger("ci-controller")


def _now() -> float:
    return time.time()


class Controller:
    def __init__(
        self,
        config: ControllerConfig,
        github: GitHubAdapter,
        docker: DockerAdapter,
        ledger: Ledger,
        metrics: MetricsStore | None = None,
        host_stats_reader: Callable[[], HostStats] | None = None,
    ) -> None:
        self.config = config
        self.github = github
        self.docker = docker
        self.ledger = ledger
        self._last_decisions: list[Decision] = []
        self.metrics = metrics
        self._host_stats_reader = host_stats_reader
        self._config_version = config.config_version()
        self._peaks: dict[str, tuple[int, float]] = {}

    def _emit(self, **fields: object) -> None:
        if self.metrics is None:
            return
        try:
            self.metrics.record_event(config_version=self._config_version, **fields)  # type: ignore[arg-type]
        except Exception as exc:  # metrics must never break the loop
            log.warning("metrics write failed: %s", exc)

    def reconcile(self) -> None:
        running = {lane.lane_id: lane for lane in self.docker.list_lanes()}
        # Sample live lanes and update peak footprint.
        for lane in running.values():
            try:
                sampled = self.docker.sample(lane.container_id)
            except Exception:  # sampling must never break the loop
                sampled = None
            if sampled is not None:
                prev = self._peaks.get(lane.lane_id, (0, 0.0))
                self._peaks[lane.lane_id] = (max(prev[0], sampled[0]), max(prev[1], sampled[1]))
        # Drop ledger entries whose lane is gone (frees budget); emit reap events.
        for lane_id in self.ledger.lane_ids() - set(running):
            res = next((r for r in self.ledger.reservations() if r.lane_id == lane_id), None)
            peak = self._peaks.pop(lane_id, (None, None))
            if res is not None:
                self._emit(
                    kind="reap",
                    job_id=res.job_id,
                    repo=res.repo,
                    class_name=res.class_name,
                    lane_id=lane_id,
                    ts=_now(),
                    peak_ram_mb=peak[0],
                    peak_cpu_pct=peak[1],
                )
            self.ledger.remove(lane_id)
        # Re-adopt running lanes the ledger doesn't know about (post-restart).
        known_jobs = {r.job_id for r in self.ledger.reservations()}
        for lane in running.values():
            if lane.job_id in known_jobs:
                continue
            self._readopt(lane.lane_id, lane.job_id)

    def _readopt(self, lane_id: str, job_id: int) -> None:
        # Best-effort: we cannot recover the repo/labels from the lane label set,
        # so reserve the default class's footprint (a safe over- or exact-estimate
        # for the common light lane). Heavy lanes self-correct when they finish.
        job_class = self.config.classes[self.config.default_class]
        self.ledger.add(
            Reservation(
                lane_id=lane_id,
                job_id=job_id,
                repo="(adopted)",
                class_name=self.config.default_class,
                ram_mb=job_class.ram_mb,
                needs_kvm=job_class.needs_kvm,
                work_disk=job_class.work_disk,
                work_gb=job_class.work_gb,
            )
        )

    def tick(self) -> list[Decision]:
        self.reconcile()
        jobs: list[QueuedJob] = []
        for repo in self.config.repo_names():
            try:
                jobs.extend(self.github.list_queued_jobs(repo))
            except Exception as exc:  # one bad repo must not kill the loop
                log.warning("poll failed for %s: %s", repo, exc)

        host_stats = None
        if self._host_stats_reader is not None:
            try:
                host_stats = self._host_stats_reader()
            except Exception as exc:
                log.warning("host stats read failed: %s", exc)

        decisions = evaluate(jobs, self.ledger, self.config, host_stats)
        for decision in decisions:
            if isinstance(decision, AdmitDecision):
                self._admit(decision)
                self._emit(
                    kind="admit",
                    job_id=decision.job.job_id,
                    repo=decision.job.repo,
                    class_name=decision.class_name,
                    work_disk=decision.work_disk,
                    ts=_now(),
                )
            else:
                self._emit(
                    kind="defer",
                    job_id=decision.job.job_id,
                    repo=decision.job.repo,
                    reason=decision.reason,
                    ts=_now(),
                )
        self._last_decisions = decisions
        return decisions

    def _admit(self, decision: AdmitDecision) -> None:
        try:
            token = self.github.mint_registration_token(decision.job.repo)
            lane_id = self.docker.spawn(decision, registration_token=token)
        except Exception as exc:  # spawn failure: leave job queued
            log.error("failed to spawn lane for job %s: %s", decision.job.job_id, exc)
            return
        self.ledger.add(
            Reservation(
                lane_id=lane_id,
                job_id=decision.job.job_id,
                repo=decision.job.repo,
                class_name=decision.class_name,
                ram_mb=decision.ram_mb,
                needs_kvm=decision.needs_kvm,
                work_disk=decision.work_disk,
                work_gb=decision.work_gb,
            )
        )
        log.info(
            "admitted job %s (%s) on lane %s", decision.job.job_id, decision.class_name, lane_id
        )

    def status(self) -> dict:
        deferred = [d for d in self._last_decisions if isinstance(d, DeferDecision)]
        return {
            "budget_ram_mb": self.config.ram_budget_mb,
            "ledger_ram_mb": self.ledger.total_ram(),
            "lanes_running": self.ledger.lane_count(),
            "max_lanes": self.config.max_concurrent_lanes,
            "kvm_in_use": self.ledger.kvm_in_use(),
            "disk_gb": {
                disk: {
                    "used": self.ledger.disk_gb_in_use(disk),
                    "budget": budget,
                }
                for disk, budget in self.config.disk_budget_gb.items()
            },
            "running": [
                {
                    "lane_id": r.lane_id,
                    "job_id": r.job_id,
                    "repo": r.repo,
                    "class": r.class_name,
                    "ram_mb": r.ram_mb,
                }
                for r in self.ledger.reservations()
            ],
            "deferred": [
                {"job_id": d.job.job_id, "repo": d.job.repo, "reason": d.reason} for d in deferred
            ],
        }
