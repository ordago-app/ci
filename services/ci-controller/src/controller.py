from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from src.config import ControllerConfig
from src.docker_adapter import DockerPool, LaneInfo
from src.github_adapter import RUNS_SCAN_LIMIT, GitHubAdapter
from src.host_stats import HostStats
from src.ledger import Ledger
from src.metrics import MetricsStore
from src.models import (
    BUDGET_FULL,
    DISK_FULL,
    INFRA_FAILURE,
    KVM_BUSY,
    LANE_CEILING,
    LANE_LOST_UNATTRIBUTED,
    AdmitDecision,
    Decision,
    DeferDecision,
    QueuedJob,
    Reservation,
)
from src.scheduler import LocalScheduler, Scheduler

log = logging.getLogger("ci-controller")

# Consecutive failed attribution attempts for one lane before we say so. At the 15 s
# poll interval this is five minutes of a lane GitHub calls busy whose job we cannot
# name — far longer than the seconds it normally takes for a run to appear in the
# in_progress list, and short enough to still be in the log while the lane exists.
ATTRIBUTION_WARN_AFTER = 20

# A stuck lane re-blocks every tick; emit the 1st and then every Nth so the streak is
# visible in the events table without one row per tick per lane.
_REAP_BLOCK_EMIT_EVERY = 20

_PRESSURE_GATES = frozenset({BUDGET_FULL, DISK_FULL, LANE_CEILING})
# A defer binding one of these cannot be relieved by reaping a lane: freeing RAM or a
# lane slot does not free the KVM device. Counting it as pressure would make the
# reaper destroy a warm lane every tick while the job keeps deferring on the gate
# reaping can never touch.
_UNRELIEVABLE_GATES = frozenset({KVM_BUSY})


def _now() -> float:
    return time.time()


def _lane_state(res: Reservation, idle_cap: float) -> str:
    """busy | stuck | booting.

    "stuck" is any unattributed lane the reaper has actually declined to remove, or one
    that is past the idle cap and so should have been removed by now. Both mean the same
    thing to a reader: this lane is holding RAM and a slot and is not going to give them
    back on its own."""
    if res.running_job_id is not None:
        return "busy"
    if res.reap_blocked_reason is not None:
        return "stuck"
    if res.idle_since is not None and _now() - res.idle_since >= idle_cap:
        return "stuck"
    return "booting"


def _idle_for(res: Reservation, now: float) -> float:
    """Seconds a lane has been idle. Callers only pass reservations already filtered
    on `idle_since is not None`."""
    return now - res.idle_since  # type: ignore[operator]


def _on_host(lanes: list[Reservation], host: str | None) -> list[Reservation]:
    return [r for r in lanes if host is None or r.host == host]


def _total_ram(lanes: list[Reservation]) -> int:
    return sum(r.ram_mb for r in lanes)


def _kvm_in_use(lanes: list[Reservation]) -> bool:
    return any(r.needs_kvm for r in lanes)


def _disk_gb_in_use(lanes: list[Reservation], disk: str) -> int:
    return sum(r.work_gb for r in lanes if r.work_disk == disk)


class Controller:
    def __init__(
        self,
        config: ControllerConfig,
        github: GitHubAdapter,
        docker: DockerPool,
        *,
        host: str,
        ledger: Ledger | None = None,
        metrics: MetricsStore | None = None,
        host_stats_reader: Callable[[], HostStats] | None = None,
        scheduler: Scheduler | None = None,
    ) -> None:
        if scheduler is None:
            scheduler = LocalScheduler(config=config, ledger=ledger or Ledger())
        elif ledger is not None:
            raise ValueError("pass either `ledger` or `scheduler`, not both")
        self.config = config
        self.github = github
        self.docker = docker
        self.scheduler = scheduler
        self._last_decisions: list[Decision] = []
        # job_id -> the epoch second we FIRST saw it deferred, so /status can say how long
        # a job has been waiting. Deliberately in-memory and therefore reset by a restart:
        # the durable answer lives in the metrics DB's defer events, and reading that on
        # every /status would put a SQL query behind a page that polls every 5s. Pruned
        # each tick to the jobs still queued, so it cannot grow without bound.
        self._queued_since: dict[int, float] = {}
        self.metrics = metrics
        self._host_stats_reader = host_stats_reader
        self._config_version = config.config_version()
        self._peaks: dict[str, tuple[int, float]] = {}
        # The controller's OWN host — used to decide which host's /proc it can read
        # (host_stats) and which host's work-dir filesystem it can see (_reap_work_dir).
        # It is no longer stamped onto every event: events.host now records the LANE's
        # host (see reconcile()), which is the whole point of the per-host report.
        self._host = host
        # What _sync_lane_state actually learned THIS tick: which repos' runner lists
        # it successfully fetched, and every runner name seen across those successful
        # calls. This is the only basis on which an unregistered lane's absence may be
        # inferred — "we never heard about it" (empty because sync failed, or the repo
        # is unconfigured) must never be read as "GitHub confirms it's gone".
        self._synced_repos: set[str] = set()
        self._seen_runner_names: set[str] = set()
        # Per lane, how many consecutive polls have found it busy but failed to name the
        # job it is running. A lane stuck here is invisible otherwise: /status shows it
        # booting forever and its reap lands unattributed.
        self._unresolved_attributions: dict[str, int] = {}
        # Populated by reconcile() from docker.snapshot(); status() reads the cached
        # value rather than pinging every host on every /status request.
        self._healthy: set[str] = set()
        # Consecutive failed health checks per host. Declaring a host lost REAPS every
        # lane it holds, so that verdict is debounced; skipping it for admission is not.
        self._unhealthy_ticks: dict[str, int] = {}

    @property
    def ledger(self) -> Ledger:
        """The scheduler's ledger, for tests and status rendering.

        Available only while the scheduler runs in-process. A remote scheduler has no
        local ledger to expose; this property raises there rather than returning a
        hollow one, so a caller that still needs direct ledger access fails loudly
        instead of silently reading an empty set."""
        ledger = getattr(self.scheduler, "ledger", None)
        if ledger is None:
            raise AttributeError("this scheduler has no in-process ledger")
        return ledger

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

    def _sync_lane_state(self) -> None:
        """Join the ledger to GitHub's runner list by name (RUNNER_NAME is the lane_id) and,
        on the idle->busy edge, resolve which job the lane was actually handed.

        Every configured repo is polled, not just the ones with known lanes: that is how a
        lane recovered without a repo label finds its own repo again.

        Also records, on `self`, which repos' lists actually succeeded this tick and
        which runner names were seen — the idle reaper's only valid basis for treating
        an unregistered lane as confirmed absent rather than merely unheard-from."""
        self._synced_repos = set()
        self._seen_runner_names = set()
        # One snapshot for the whole loop, not one per repo: a lane_id appears in at most
        # one repo's runner listing per tick, so nothing below can invalidate it, and it
        # collapses N scheduler round-trips into one.
        lanes = self.scheduler.lanes()
        lane_ids = {r.lane_id for r in lanes}
        self._unresolved_attributions = {
            lane_id: n
            for lane_id, n in self._unresolved_attributions.items()
            if lane_id in lane_ids
        }
        if not lane_ids:
            return
        for repo in self.config.repo_names():
            try:
                listing = self.github.list_runners(repo)
            except Exception as exc:  # one bad repo must not kill the loop
                log.warning("runner list failed for %s: %s", repo, exc)
                continue
            runners = {r.name: r for r in listing.runners}
            # A truncated listing is still fine to attribute FROM (a runner it does name
            # is really there), but never to infer absence from: the lane it fails to
            # mention may simply be on a page we did not fetch.
            if listing.complete:
                self._synced_repos.add(repo)
            self._seen_runner_names.update(runners)
            for res in lanes:
                info = runners.get(res.lane_id)
                if info is None:
                    continue
                if res.runner_id != info.runner_id or res.repo != repo:
                    self.scheduler.update(res.lane_id, runner_id=info.runner_id, repo=repo)
                if not info.busy or res.running_job_id is not None:
                    continue
                try:
                    lookup = self.github.find_job_for_runner(repo, res.lane_id)
                except Exception as exc:
                    log.warning("attribution failed for lane %s: %s", res.lane_id, exc)
                    continue
                running = lookup.job
                if running is None:
                    self._note_unresolved_attribution(res.lane_id, complete=lookup.complete)
                    continue  # busy but not yet visible in the runs list; retry next tick
                self._unresolved_attributions.pop(res.lane_id, None)
                self.scheduler.update(
                    res.lane_id,
                    running_job_id=running.job_id,
                    workflow=running.workflow,
                    job_name=running.job_name,
                    idle_since=None,
                    reap_blocked_reason=None,
                    reap_block_count=0,
                )
                log.info(
                    "lane %s spawned for job %s is running job %s",
                    res.lane_id,
                    res.spawned_for_job_id,
                    running.job_id,
                )
                self._emit(
                    kind="attach",
                    job_id=running.job_id,
                    spawned_for_job_id=res.spawned_for_job_id,
                    attributed=1,
                    repo=repo,
                    class_name=res.class_name,
                    lane_id=res.lane_id,
                    ts=_now(),
                    workflow=running.workflow,
                    job_name=running.job_name,
                )

    def _note_unresolved_attribution(self, lane_id: str, *, complete: bool) -> None:
        """Count a poll that found a lane busy but could not name its job, and say so
        once the count stops being explainable by "the run isn't listed yet".

        An incomplete scan is said out loud on the FIRST occurrence rather than after
        ATTRIBUTION_WARN_AFTER: it is not the benign "the run has not appeared yet" case
        the counter exists to wait out, it is us having stopped looking. Waiting 20 polls
        to mention that is how the missing jobs-endpoint pagination stayed invisible.
        """
        attempts = self._unresolved_attributions.get(lane_id, 0) + 1
        self._unresolved_attributions[lane_id] = attempts
        if not complete:
            log.warning(
                "lane %s is busy but its job could not be named from an INCOMPLETE scan "
                "(a run's job list was truncated); this is not evidence the lane has no "
                "job, and attribution will keep retrying",
                lane_id,
            )
            return
        if attempts % ATTRIBUTION_WARN_AFTER:
            return
        log.warning(
            "lane %s has been busy but unattributed for %s consecutive polls: "
            "find_job_for_runner scans only the %s most recent in_progress runs, so "
            "under a backlog this lane's run can sit off that page indefinitely. It will "
            "keep reporting as booting and reap unattributed until it resolves.",
            lane_id,
            attempts,
            RUNS_SCAN_LIMIT,
        )

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
        self._sync_lane_state()
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
        reservations = self.scheduler.lanes()
        for lane_id in {r.lane_id for r in reservations} - set(running):
            res = next((r for r in reservations if r.lane_id == lane_id), None)
            # Its host is unresponsive but not yet declared lost: the lane is invisible
            # only because we cannot ask. Hold the reservation — do not free the budget
            # and do not record an outcome — until the host either answers again or
            # crosses the miss threshold.
            if res is not None and res.host not in self._healthy and res.host not in lost:
                continue
            peak = self._peaks.pop(lane_id, (None, None))
            if res is not None:
                # Each of "the host went away", "recovered after a restart", "never
                # attributed" and "the lookup failed" gets its own sentinel string,
                # distinct from a genuine NULL conclusion (job reached no terminal
                # outcome) — ci_bench.py's infra_failures()/lookup_failures() depend on
                # telling them apart.
                if res.host in lost:
                    # The HOST is gone (desktop slept, WSL shut down), not the job —
                    # every lane it held vanishes with it. Recorded distinctly, and
                    # without a job_conclusion() lookup: the job never reached a
                    # terminal outcome, so a lookup would be a wasted API call per
                    # lost lane per tick. See §2e in the plan.
                    #
                    # Which sentinel depends on whether we ever observed what this lane
                    # was running. Only an attributed lane can carry a JOB-level verdict:
                    # on an unattributed one, job_id is still the spawned-for prediction,
                    # and blaming a job that may never have run here is the very defect
                    # this branch removes. Deciding it at the write site is what lets
                    # infra_failures() count the sentinel unconditionally.
                    conclusion: str | None = (
                        INFRA_FAILURE if res.running_job_id is not None else LANE_LOST_UNATTRIBUTED
                    )
                elif res.repo == "(adopted)":
                    # "(adopted)" is a sentinel for lanes recovered after a restart, not
                    # a real repo — looking it up would 404 on every tick.
                    conclusion = "adopted"
                elif res.running_job_id is None:
                    # claimed_job_id is still the spawned-for prediction here, so a
                    # lookup would describe a job this lane never ran — and whatever it
                    # returned (usually the other lane's `success`) would be written to
                    # this row permanently. Skip it; that also saves an API call.
                    conclusion = "unattributed"
                else:
                    try:
                        conclusion = self.github.job_conclusion(res.repo, res.claimed_job_id)
                    except Exception as exc:  # classification must never break reconcile
                        log.warning(
                            "conclusion lookup failed for job %s: %s", res.claimed_job_id, exc
                        )
                        conclusion = "lookup_failed"
                self._emit(
                    kind="reap",
                    job_id=res.claimed_job_id,
                    repo=res.repo,
                    class_name=res.class_name,
                    lane_id=lane_id,
                    ts=_now(),
                    peak_ram_mb=peak[0],
                    peak_cpu_pct=peak[1],
                    conclusion=conclusion,
                    job_name=res.job_name,
                    workflow=res.workflow,
                    spawned_for_job_id=res.spawned_for_job_id,
                    attributed=1 if res.running_job_id is not None else None,
                    host=res.host,
                )
                if res.host == self._host:
                    self._reap_work_dir(lane_id, res.work_disk)
                # else: nothing to do, and deliberately so. The controller can only
                # unlink paths on its OWN filesystem, so a remote host must not depend on
                # it: those hosts run work_dir_mode=volume and dockerd drops the lane's
                # anonymous volume with the container (ADR 0023). A remote host left in
                # bind mode would leak a workspace per killed lane, which is why
                # test_deploy_wiring asserts none is.
            self.scheduler.release(lane_id)
        # Re-adopt running lanes the ledger doesn't know about (post-restart). Identity is
        # lane_id, not job_id: an attributed lane's claimed_job_id no longer matches the
        # job_id on its own Docker label, and it must not be mistaken for an orphan.
        known_lane_ids = {r.lane_id for r in self.scheduler.lanes()}
        for lane in running.values():
            if lane.lane_id in known_lane_ids:
                # Refresh the start time from the daemon every tick rather than stamping
                # it once at spawn. The daemon is the only source that survives a
                # controller restart, so "how long has this been running" stays true
                # across one instead of resetting to zero under a lane that never stopped.
                if lane.started_at is not None:
                    self.scheduler.update(lane.lane_id, started_at=lane.started_at)
                continue
            host = lane.host or listed_on[lane.lane_id]
            self._readopt(
                lane.lane_id,
                lane.job_id,
                host,
                lane.class_name,
                lane.container_id,
                lane.started_at,
            )

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

    def _readopt(
        self,
        lane_id: str,
        job_id: int,
        host: str,
        class_name: str | None,
        container_id: str,
        started_at: float | None = None,
    ) -> None:
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
        self.scheduler.adopt(
            Reservation(
                lane_id=lane_id,
                spawned_for_job_id=job_id,
                repo="(adopted)",
                class_name=resolved,
                ram_mb=job_class.ram_mb,
                needs_kvm=job_class.needs_kvm,
                work_disk=job_class.work_disk,
                work_gb=job_class.work_gb,
                host=host,
                # The reaper needs both: an idle clock to age the lane against, and the
                # container to remove if it never takes a job.
                idle_since=_now(),
                container_id=container_id,
                started_at=started_at,
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

        decisions = self.scheduler.plan(jobs, host_stats, self._healthy)
        for decision in decisions:
            if isinstance(decision, AdmitDecision):
                if not self._admit(decision):
                    continue  # the job stays queued; an admit event here would be fiction
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
                    # NULL before #157, so no historical defer row can be grouped by class.
                    # Rows from here on can: "what was the queue made of" is a question the
                    # capacity report should be able to answer, not just "how long was it".
                    class_name=decision.class_name,
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
        self._track_queue_age(decisions)
        self._reap_idle_lanes(decisions)
        return decisions

    def _track_queue_age(self, decisions: list[Decision]) -> None:
        # First-seen wins: a job deferred on five consecutive ticks has been waiting since
        # the first, not the last. Rebuilding the dict from this tick's defers is also what
        # prunes it — a job that got admitted or vanished from GitHub's queue drops out.
        now = _now()
        self._queued_since = {
            d.job.job_id: self._queued_since.get(d.job.job_id, now)
            for d in decisions
            if isinstance(d, DeferDecision)
        }

    def _reap_idle_lanes(self, decisions: list[Decision]) -> None:
        """Reclaim reserves held by lanes that never got a job.

        Pressure is read from every binding gate, not just the first one: #106 made
        DeferDecision carry all of them, so a budget_full masked by lane_ceiling still
        counts. A defer that also binds an unrelievable gate (kvm_busy) does not count
        as pressure: reaping a lane would not relieve it, so the job would just defer
        again next tick while we burned a warm lane for nothing.

        A lane on a host that did not answer this tick is never a candidate. We cannot
        reach its daemon to remove the container, and — the reason that matters — we
        cannot tell "the lane is idle" from "the host is unreachable". reconcile() takes
        the same position: an unreachable host's lanes keep their reservation rather
        than being read as vanished. Reaping here would be the second, contradictory
        mechanism, freeing budget for a lane that is very likely still running."""
        now = _now()
        idle = sorted(
            (
                r
                for r in self.scheduler.lanes()
                if r.running_job_id is None and r.idle_since is not None and r.host in self._healthy
            ),
            key=lambda r: r.idle_since,  # type: ignore[arg-type,return-value]
        )
        # Past the absolute cap a lane is not warm capacity, it is a leak: always reaped.
        targets = [r for r in idle if _idle_for(r, now) >= self.config.idle_lane_max_seconds]
        pressure = sum(
            1
            for d in decisions
            if isinstance(d, DeferDecision)
            and _PRESSURE_GATES.intersection(d.reasons)
            and not _UNRELIEVABLE_GATES.intersection(d.reasons)
        )
        if pressure:
            capped = {r.lane_id for r in targets}
            eligible = [
                r
                for r in idle  # sorted longest-idle first
                if r.lane_id not in capped and _idle_for(r, now) >= self.config.idle_grace_seconds
            ]
            targets.extend(eligible[:pressure])
        for res in targets:
            self._reap_idle_lane(res, now)

    def _confirmed_absent(self, res: Reservation) -> bool:
        """True only when this tick's runner-list sync actually consulted GitHub about
        this lane and it was not there — a positive absence signal. False for "we
        never heard about it", whether because the sync failed, the lane's repo isn't
        (yet) configured, or the tick never got that far — none of those are evidence
        the lane is gone."""
        if res.repo == "(adopted)":
            # No single repo owns this lane: only trust it if every configured repo
            # was actually polled this tick and none of them listed it.
            if not self.config.repo_names() <= self._synced_repos:
                return False
        elif res.repo not in self._synced_repos:
            return False
        return res.lane_id not in self._seen_runner_names

    def _block_reap(self, res: Reservation, reason: str, now: float) -> None:
        """Record that this tick declined to tear `res` down, and why.

        Every early return in _reap_idle_lane is a deferral to some other authority --
        GitHub's 422, a failed API call, an absent positive-absence signal, a Docker
        remove that raised. Each is individually correct: none of them is evidence the
        lane is safe to destroy. What was missing is that they are also unbounded. The
        lane keeps its full reservation and its slot while the same refusal repeats every
        tick, and nothing outside the log ever says so.

        Recording the reason on the reservation is what lets status() stop calling such a
        lane "booting", and the event is what lets ci_bench count it. Neither changes the
        refusal itself -- inferring "gone" from a refusal is the exact mistake the guards
        exist to prevent."""
        first = res.reap_blocked_reason != reason
        count = 1 if first else res.reap_block_count + 1
        self.scheduler.update(res.lane_id, reap_blocked_reason=reason, reap_block_count=count)
        # Emit on the first block and then sparsely: one row per tick per stuck lane would
        # be ~200 rows for the 2026-08-20 leak alone, and the signal is the streak, not
        # each repetition.
        if first or count % _REAP_BLOCK_EMIT_EVERY == 0:
            self._emit(
                kind="idle_reap_blocked",
                job_id=res.spawned_for_job_id,
                spawned_for_job_id=res.spawned_for_job_id,
                attributed=None,
                repo=res.repo,
                class_name=res.class_name,
                lane_id=res.lane_id,
                ts=now,
                reason=reason,
                host=res.host,
            )
        log.warning(
            "idle lane %s (%s, %s MB) is %.0fs idle -- past the %.0fs cap -- and its "
            "teardown has been declined %s consecutive tick(s): %s. It is holding its "
            "full reservation while blocked.",
            res.lane_id,
            res.class_name,
            res.ram_mb,
            _idle_for(res, now),
            self.config.idle_lane_max_seconds,
            count,
            reason,
        )

    def _reap_idle_lane(self, res: Reservation, now: float) -> None:
        if res.runner_id is not None and res.repo != "(adopted)":
            try:
                if not self.github.delete_runner(res.repo, res.runner_id):
                    # 422: GitHub says the runner is busy right now. That can mean it
                    # took a job in the race window, or it has been running one all
                    # along and we just haven't attributed it yet — either way GitHub,
                    # not our idle clock, gets the final say on whether it's safe.
                    self._block_reap(res, "deregistration_refused", now)
                    return
            except Exception as exc:  # a reap failure must never break the loop
                log.warning("deregister failed for lane %s: %s", res.lane_id, exc)
                self._block_reap(res, "deregister_error", now)
                return
        else:
            if _idle_for(res, now) < self.config.idle_lane_max_seconds:
                return  # never registered: tear down only once past the absolute cap
            if not self._confirmed_absent(res):
                # Never registered AND no positive absence signal this tick: "we
                # never heard about it" is not "it's gone" (sync failure, restart
                # before the first sync, a repo dropped from config, ...). Refuse.
                log.warning(
                    "lane %s is unregistered and past the idle cap, but GitHub's "
                    "runner list did not confirm it absent this tick; refusing to "
                    "tear it down without a positive absence signal",
                    res.lane_id,
                )
                self._block_reap(res, "absence_unconfirmed", now)
                return
            # Degraded path: no GitHub deregistration confirmed this teardown, we are
            # acting on inferred absence instead.
            log.warning(
                "lane %s confirmed absent from GitHub's runner list; reaping on "
                "inferred absence (no registration to deregister)",
                res.lane_id,
            )
        if res.container_id is None:
            # Nothing for us to remove, but the ledger entry is about to be dropped —
            # if a container actually exists under this lane_id, reconcile() will
            # re-adopt it next tick with a fresh idle clock instead of us reaping it
            # now. Surface that rather than silently proceeding.
            log.warning(
                "idle lane %s has no container_id on record; dropping its ledger "
                "entry without removing a container",
                res.lane_id,
            )
        else:
            try:
                self.docker.for_host(res.host).remove(res.container_id)
            except Exception as exc:
                log.warning("failed to remove idle lane %s: %s", res.lane_id, exc)
                self._block_reap(res, "container_remove_failed", now)
                return
        self._emit(
            kind="idle_reap",
            job_id=res.spawned_for_job_id,
            spawned_for_job_id=res.spawned_for_job_id,
            attributed=None,
            repo=res.repo,
            class_name=res.class_name,
            lane_id=res.lane_id,
            ts=now,
            reason="idle",
            host=res.host,
        )
        if res.host == self._host:
            self._reap_work_dir(res.lane_id, res.work_disk)
        # else: same as reconcile() — the controller can only see its OWN filesystem, so
        # a remote host reclaims its own workspaces via work_dir_mode=volume (ADR 0023).
        self.scheduler.release(res.lane_id)
        self._peaks.pop(res.lane_id, None)
        log.info("reaped idle lane %s (%s)", res.lane_id, res.class_name)

    def _admit(self, decision: AdmitDecision) -> bool:
        """True once the lane exists AND the scheduler holds a reservation for it; the
        caller emits no admit event unless this returns True.

        The two can go out of step: under `HttpScheduler` the commit is a network call, so
        a lane can be spawned and running while the reservation never lands. That returns
        False, leaving the job queued and its budget unreserved. `_readopt` closes it on a
        later tick — the container is listed on its host and re-adopted into the ledger,
        with its class read back from `CLASS_LABEL`."""
        try:
            token = self.github.mint_registration_token(decision.job.repo)
            lane_id, container_id = self.docker.for_host(decision.host).spawn(
                decision, registration_token=token
            )
            self.scheduler.commit(
                decision,
                lane_id=lane_id,
                container_id=container_id,
                idle_since=_now(),
            )
        except Exception as exc:  # spawn failure: leave job queued
            log.error("failed to spawn lane for job %s: %s", decision.job.job_id, exc)
            return False
        log.info(
            "admitted job %s (%s) on lane %s (%s)",
            decision.job.job_id,
            decision.class_name,
            lane_id,
            decision.host,
        )
        return True

    def status(self) -> dict:
        lanes = self.scheduler.lanes()
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
            on_host = _on_host(lanes, name)
            hosts[name] = {
                "lanes": len(on_host),
                "max_lanes": max_lanes,
                "ram_mb": _total_ram(on_host),
                "budget_ram_mb": budget_ram_mb,
                "healthy": name in self._healthy,
            }
        return {
            "budget_ram_mb": self.config.ram_budget_mb,
            "ledger_ram_mb": _total_ram(lanes),
            "lanes_running": len(lanes),
            "max_lanes": self.config.max_concurrent_lanes,
            "kvm_in_use": _kvm_in_use(lanes),
            "config_version": self._config_version,
            "admission_mode": self.config.admission_mode,
            "disk_gb": {
                disk: {
                    "used": _disk_gb_in_use(lanes, disk),
                    "budget": budget,
                }
                for disk, budget in self.config.disk_budget_gb.items()
            },
            "running": [
                {
                    "lane_id": r.lane_id,
                    "job_id": r.running_job_id,
                    "spawned_for_job_id": r.spawned_for_job_id,
                    "repo": r.repo,
                    "class": r.class_name,
                    "ram_mb": r.ram_mb,
                    # Which box is actually running it. Derivable from the lane_id prefix,
                    # but only by string-splitting a field that is an opaque id by contract;
                    # a reader that wants per-host breakdowns should be given the field.
                    "host": r.host,
                    "workflow": r.workflow,
                    "job_name": r.job_name,
                    # Three states, not two. "booting" used to absorb every unattributed
                    # lane, including one that had blown through idle_lane_max_seconds and
                    # was stuck holding its reserve -- which is how the 2026-08-20 node_heavy
                    # leak read as a healthy lane in `make ci-status` for 36 minutes.
                    "state": _lane_state(r, self.config.idle_lane_max_seconds),
                    "reap_blocked_reason": r.reap_blocked_reason,
                    "reap_block_count": r.reap_block_count,
                    "idle_seconds": (None if r.idle_since is None else int(_now() - r.idle_since)),
                    # Wall-clock age of the lane's container. None when the daemon gave no
                    # usable timestamp — the reader shows "unknown", never a zero that
                    # would read as "just started".
                    "running_seconds": (
                        None if r.started_at is None else max(0, int(_now() - r.started_at))
                    ),
                }
                for r in lanes
            ],
            "deferred": [
                {
                    "job_id": d.job.job_id,
                    "repo": d.job.repo,
                    "reason": d.reason,
                    # Every binding gate, not just the first. `reason` stays the primary one
                    # so existing readers are unaffected.
                    "reasons": list(d.reasons),
                    "class": d.class_name,
                    "workflow": d.job.workflow,
                    "job_name": d.job.job_name,
                    # The closest host, i.e. whose gates `reasons` describes. None for the
                    # non-capacity defers, which belong to no host — same rule as the event.
                    "host": d.host,
                    # Since this controller first saw the job deferred, so it resets on a
                    # restart rather than pretending to know a history it no longer holds.
                    "waiting_seconds": (
                        None
                        if d.job.job_id not in self._queued_since
                        else max(0, int(_now() - self._queued_since[d.job.job_id]))
                    ),
                }
                for d in deferred
            ],
            "hosts": hosts,
        }
