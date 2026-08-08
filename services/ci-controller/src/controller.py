from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path

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
        host: str = "powerserver",
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
        # Stamped on every event so the per-host report path can populate. Without it
        # events.host stays NULL forever and that section is dead surface.
        self._host = host

    def _emit(self, **fields: object) -> None:
        if self.metrics is None:
            return
        try:
            self.metrics.record_event(
                config_version=self._config_version,
                host=self._host,
                **fields,  # type: ignore[arg-type]
            )
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
                # "(adopted)" is a sentinel for lanes recovered after a restart, not a
                # real repo — looking it up would 404 on every tick. Both this and a
                # failed lookup get their own sentinel string, distinct from a genuine
                # NULL conclusion (job reached no terminal outcome) — ci_bench.py's
                # infra_failures()/lookup_failures() depend on telling the three apart.
                if res.repo == "(adopted)":
                    conclusion: str | None = "adopted"
                else:
                    try:
                        conclusion = self.github.job_conclusion(res.repo, res.job_id)
                    except Exception as exc:  # classification must never break reconcile
                        log.warning("conclusion lookup failed for job %s: %s", res.job_id, exc)
                        conclusion = "lookup_failed"
                self._emit(
                    kind="reap",
                    job_id=res.job_id,
                    repo=res.repo,
                    class_name=res.class_name,
                    lane_id=lane_id,
                    ts=_now(),
                    peak_ram_mb=peak[0],
                    peak_cpu_pct=peak[1],
                    conclusion=conclusion,
                    job_name=res.job_name,
                    workflow=res.workflow,
                )
                self._reap_work_dir(lane_id, res.work_disk)
            self.ledger.remove(lane_id)
        # Re-adopt running lanes the ledger doesn't know about (post-restart).
        known_jobs = {r.job_id for r in self.ledger.reservations()}
        for lane in running.values():
            if lane.job_id in known_jobs:
                continue
            self._readopt(lane.lane_id, lane.job_id, lane.class_name)

    def _reap_work_dir(self, lane_id: str, work_disk: str) -> None:
        # The ephemeral runner auto-removes its container but leaves the per-lane
        # host work dir behind (DockerAdapter binds work_dirs[disk] and the runner
        # mkdir's <lane_id>-work inside it). Delete it when we reap the lane, or the
        # disk fills one stale dir per job. work_dirs[disk] must be mounted into the
        # controller (see compose.yml) for this path to be visible.
        work_base = self.config.work_dirs.get(work_disk)
        if work_base is None:
            return
        base = Path(work_base).resolve()
        work_dir = (base / f"{lane_id}-work").resolve()
        # lane_id can originate from an unconstrained Docker label (re-adopted lanes),
        # so a `../` could otherwise escape the base. Refuse anything not directly under it.
        if work_dir.parent != base:
            log.warning(
                "refusing to clean work dir outside base: lane_id=%r -> %s", lane_id, work_dir
            )
            return
        try:
            shutil.rmtree(work_dir)
        except FileNotFoundError:
            pass  # lane never wrote a work dir (or already cleaned) — nothing to do
        except OSError as exc:  # never let cleanup break the reconcile loop
            log.warning("failed to remove work dir %s: %s", work_dir, exc)

    def _readopt(self, lane_id: str, job_id: int, class_name: str | None) -> None:
        # The repo is unrecoverable from the label set, so it stays the "(adopted)"
        # sentinel (reap skips the conclusion lookup for it). The class is recoverable:
        # it is stamped on the container at spawn. Re-adopting at default_class instead
        # booked multi-GB lanes at the cheapest reserve — observed twice in production,
        # 7084 MB and 7100 MB peaks against a 700 MB reservation, which hands the
        # difference to new admissions and lands the host in the OOM path.
        resolved = class_name if class_name in self.config.classes else None
        if resolved is None:
            # Pre-CLASS_LABEL container, or a class dropped from the config since spawn.
            # Reserve the ceiling: over-reserving costs deferrals, which are free and
            # recoverable; under-reserving is what kills the host.
            resolved = max(self.config.classes, key=lambda name: self.config.classes[name].ram_mb)
            log.warning(
                "lane %s (job %s) has no known class label %r; "
                "re-adopting at the largest class '%s'",
                lane_id,
                job_id,
                class_name,
                resolved,
            )
        job_class = self.config.classes[resolved]
        self.ledger.add(
            Reservation(
                lane_id=lane_id,
                job_id=job_id,
                repo="(adopted)",
                class_name=resolved,
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
                    job_name=decision.job.job_name,
                    workflow=decision.job.workflow,
                )
            else:
                self._emit(
                    kind="defer",
                    job_id=decision.job.job_id,
                    repo=decision.job.repo,
                    reason=decision.reason,
                    reasons=",".join(decision.reasons),
                    ts=_now(),
                    job_name=decision.job.job_name,
                    workflow=decision.job.workflow,
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
                workflow=decision.job.workflow,
                job_name=decision.job.job_name,
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
            "config_version": self._config_version,
            "admission_mode": self.config.admission_mode,
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
