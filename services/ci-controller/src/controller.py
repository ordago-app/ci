from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from src.admission import evaluate
from src.config import ControllerConfig
from src.docker_adapter import DockerPool, LaneInfo
from src.github_adapter import GitHubAdapter
from src.host_stats import HostStats
from src.ledger import Ledger
from src.metrics import MetricsStore
from src.models import (
    INFRA_FAILURE,
    AdmitDecision,
    Decision,
    DeferDecision,
    QueuedJob,
    Reservation,
)

log = logging.getLogger("ci-controller")


def _now() -> float:
    return time.time()


class Controller:
    def __init__(
        self,
        config: ControllerConfig,
        github: GitHubAdapter,
        docker: DockerPool,
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
        # The controller's OWN host — used to decide which host's /proc it can read
        # (host_stats) and which host's work-dir filesystem it can see (_reap_work_dir).
        # It is no longer stamped onto every event: events.host now records the LANE's
        # host (see reconcile()), which is the whole point of the per-host report.
        self._host = host
        # Populated by reconcile() from docker.snapshot(); status() reads the cached
        # value rather than pinging every host on every /status request.
        self._healthy: set[str] = set()
        # Consecutive failed health checks per host. Declaring a host lost REAPS every
        # lane it holds, so that verdict is debounced; skipping it for admission is not.
        self._unhealthy_ticks: dict[str, int] = {}

    def _emit(self, **fields: object) -> None:
        if self.metrics is None:
            return
        try:
            self.metrics.record_event(
                config_version=self._config_version,
                **fields,  # type: ignore[arg-type]
            )
        except Exception as exc:  # metrics must never break the loop
            log.warning("metrics write failed: %s", exc)

    def _lost_hosts(self) -> set[str]:
        """Hosts that have failed enough consecutive health checks to be declared gone.

        A single failed ping is NOT enough. Reaping a host's lanes is destructive and
        irreversible: it frees their budget and writes a permanent `infra_failure` row
        that no later event corrects. But a failed ping does not mean the lanes died —
        powerserver's own socket-proxy lives in the controller's compose stack, so an
        ordinary `make deploy` recreates it and produces exactly one failing tick while
        every lane keeps running. Without this debounce that routine maintenance would
        free live lanes' budget (the same phantom-budget hazard the class-aware
        _readopt fix closed) and permanently mis-record green jobs as infra failures.

        Admission is skipped on the FIRST failure, though — that direction is cheap and
        safe, so only the destructive verdict waits.
        """
        return {
            name
            for name, misses in self._unhealthy_ticks.items()
            if misses >= self.config.host_unhealthy_ticks
        }

    def reconcile(self) -> None:
        # One observation, not two: a separate healthy() call could disagree with the
        # listing below (ping ok, list fails) and make a live host look empty.
        self._healthy, per_host_lanes = self.docker.snapshot()
        for name in self.config.resolved_hosts():
            if name in self._healthy:
                self._unhealthy_ticks[name] = 0
            else:
                self._unhealthy_ticks[name] = self._unhealthy_ticks.get(name, 0) + 1
        lost = self._lost_hosts()
        # snapshot() omits a down host's lanes, so they never appear in `running` below
        # — that is what makes them look "gone" to the ledger-diff loop, same as a lane
        # that genuinely finished. The guard there is what stops that from freeing them.

        running: dict[str, LaneInfo] = {}
        # Which host's adapter actually listed this lane — the fallback for
        # _readopt() when the lane predates HOST_LABEL.
        listed_on: dict[str, str] = {}
        for host_name, lanes in per_host_lanes.items():
            adapter = self.docker.for_host(host_name)
            for lane in lanes:
                running[lane.lane_id] = lane
                listed_on[lane.lane_id] = host_name
                try:
                    sampled = adapter.sample(lane.container_id)
                except Exception:  # sampling must never break the loop
                    sampled = None
                if sampled is not None:
                    prev = self._peaks.get(lane.lane_id, (0, 0.0))
                    self._peaks[lane.lane_id] = (
                        max(prev[0], sampled[0]),
                        max(prev[1], sampled[1]),
                    )

        # Drop ledger entries whose lane is gone (frees budget); emit reap events.
        for lane_id in self.ledger.lane_ids() - set(running):
            res = next((r for r in self.ledger.reservations() if r.lane_id == lane_id), None)
            # Its host is unresponsive but not yet declared lost: the lane is invisible
            # only because we cannot ask. Hold the reservation — do not free the budget
            # and do not record an outcome — until the host either answers again or
            # crosses the miss threshold.
            if res is not None and res.host not in self._healthy and res.host not in lost:
                continue
            peak = self._peaks.pop(lane_id, (None, None))
            if res is not None:
                if res.host in lost:
                    # The HOST is gone (desktop slept, WSL shut down), not the job —
                    # every lane it held vanishes with it. Recorded distinctly, and
                    # without a job_conclusion() lookup: the job never reached a
                    # terminal outcome, so a lookup would be a wasted API call per
                    # lost lane per tick. See §2e in the plan.
                    conclusion: str | None = INFRA_FAILURE
                elif res.repo == "(adopted)":
                    # "(adopted)" is a sentinel for lanes recovered after a restart, not
                    # a real repo — looking it up would 404 on every tick. Both this and
                    # a failed lookup get their own sentinel string, distinct from a
                    # genuine NULL conclusion (job reached no terminal outcome) —
                    # ci_bench.py's infra_failures()/lookup_failures() depend on telling
                    # the three (now four) apart.
                    conclusion = "adopted"
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
                    host=res.host,
                )
                if res.host == self._host:
                    self._reap_work_dir(lane_id, res.work_disk)
                # else: a remote host's own work dir is owned by that host's
                # stale-work-dir prune systemd unit (Task 15) — the controller can only
                # see its OWN filesystem, never a remote host's.
            self.ledger.remove(lane_id)
        # Re-adopt running lanes the ledger doesn't know about (post-restart).
        known_jobs = {r.job_id for r in self.ledger.reservations()}
        for lane in running.values():
            if lane.job_id in known_jobs:
                continue
            host = lane.host or listed_on[lane.lane_id]
            self._readopt(lane.lane_id, lane.job_id, host, lane.class_name)

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

    def _readopt(self, lane_id: str, job_id: int, host: str, class_name: str | None) -> None:
        # The repo is unrecoverable from the label set, so it stays the "(adopted)"
        # sentinel (reap skips the conclusion lookup for it). The class and the host ARE
        # recoverable: both are stamped on the container at spawn. Re-adopting at
        # default_class instead booked multi-GB lanes at the cheapest reserve — observed
        # twice in production, 7084 MB and 7100 MB peaks against a 700 MB reservation,
        # which hands the difference to new admissions and lands the host in the OOM path.
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
                host=host,
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

        # The controller can only read /proc for its OWN host — host_stats legitimately
        # has one entry. A missing entry for any other host means "no live stats", which
        # evaluate() already treats as "the host_pressure guard cannot fire there".
        host_stats: dict[str, HostStats] = {}
        if self._host_stats_reader is not None:
            try:
                host_stats[self._host] = self._host_stats_reader()
            except Exception as exc:
                log.warning("host stats read failed: %s", exc)

        decisions = evaluate(jobs, self.ledger, self.config, host_stats, self._healthy)
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
                    host=decision.host,
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
                    # The closest host, i.e. whose gates these reasons describe. NULL for
                    # the non-capacity defers (already_running, not_allowlisted,
                    # no_eligible_host), which belong to no host.
                    host=decision.host,
                )
        self._last_decisions = decisions
        return decisions

    def _admit(self, decision: AdmitDecision) -> None:
        try:
            token = self.github.mint_registration_token(decision.job.repo)
            lane_id = self.docker.for_host(decision.host).spawn(decision, registration_token=token)
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
                host=decision.host,
            )
        )
        log.info(
            "admitted job %s (%s) on lane %s (%s)",
            decision.job.job_id,
            decision.class_name,
            lane_id,
            decision.host,
        )

    def status(self) -> dict:
        deferred = [d for d in self._last_decisions if isinstance(d, DeferDecision)]
        hosts: dict[str, dict[str, int | bool]] = {}
        for name, host_cfg in self.config.resolved_hosts().items():
            # resolved_hosts() has already inherited these from the top-level config
            # when the per-host key was omitted; the fallback below only narrows the
            # Optional type away, mirroring admission._policy()'s same pattern.
            max_lanes = (
                self.config.max_concurrent_lanes
                if host_cfg.max_concurrent_lanes is None
                else host_cfg.max_concurrent_lanes
            )
            budget_ram_mb = (
                self.config.ram_budget_mb
                if host_cfg.ram_budget_mb is None
                else host_cfg.ram_budget_mb
            )
            hosts[name] = {
                "lanes": self.ledger.lane_count(name),
                "max_lanes": max_lanes,
                "ram_mb": self.ledger.total_ram(name),
                "budget_ram_mb": budget_ram_mb,
                "healthy": name in self._healthy,
            }
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
            "hosts": hosts,
        }
