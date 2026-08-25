from __future__ import annotations

from dataclasses import dataclass

from src.config import ControllerConfig, HostConfig, JobClass
from src.host_stats import HostStats
from src.ledger import Ledger
from src.models import (
    ALREADY_RUNNING,
    BUDGET_FULL,
    DISK_FULL,
    HOST_PRESSURE,
    KVM_BUSY,
    LANE_CEILING,
    NO_ELIGIBLE_HOST,
    NOT_ALLOWLISTED,
    AdmitDecision,
    Decision,
    DeferDecision,
    QueuedJob,
)


@dataclass(frozen=True)
class _HostPolicy:
    """A resolved host's limits with the optional fields narrowed."""

    name: str
    ram_budget_mb: int
    max_concurrent_lanes: int
    disk_budget_gb: dict[str, int]
    host_free_ram_floor_mb: int
    host_load_ceiling: float
    allowed_classes: frozenset[str]
    # None = unrestricted. An empty frozenset would mean "no org may use this host",
    # which is a legitimate but very different thing, so the distinction is kept.
    allowed_orgs: frozenset[str] | None


@dataclass
class _HostState:
    """A host's policy plus the capacity committed to it: the ledger's reservations for
    this host, then everything this batch has already admitted to it. Accumulation is
    per host so admitting twice to one host leaves the others' budgets untouched."""

    policy: _HostPolicy
    ram: int
    lanes: int
    kvm: bool
    disk_gb: dict[str, int]

    @property
    def headroom_mb(self) -> int:
        return self.policy.ram_budget_mb - self.ram


def _policy(host: HostConfig, config: ControllerConfig) -> _HostPolicy:
    # resolved_hosts() has already inherited every one of these from the top-level
    # config, so the fallbacks below only narrow the Optional types away; they are
    # never the value actually used.
    return _HostPolicy(
        name=host.name,
        ram_budget_mb=(config.ram_budget_mb if host.ram_budget_mb is None else host.ram_budget_mb),
        max_concurrent_lanes=(
            config.max_concurrent_lanes
            if host.max_concurrent_lanes is None
            else host.max_concurrent_lanes
        ),
        disk_budget_gb=(
            config.disk_budget_gb if host.disk_budget_gb is None else host.disk_budget_gb
        ),
        host_free_ram_floor_mb=(
            config.host_free_ram_floor_mb
            if host.host_free_ram_floor_mb is None
            else host.host_free_ram_floor_mb
        ),
        host_load_ceiling=(
            config.host_load_ceiling if host.host_load_ceiling is None else host.host_load_ceiling
        ),
        allowed_classes=frozenset(
            config.classes if host.allowed_classes is None else host.allowed_classes
        ),
        allowed_orgs=(None if host.allowed_orgs is None else frozenset(host.allowed_orgs)),
    )


def _gates(
    state: _HostState,
    job_class: JobClass,
    config: ControllerConfig,
    stats: HostStats | None,
) -> tuple[str, ...]:
    """Every capacity gate this host binds on, in the historical order.

    Evaluating EVERY gate instead of returning on the first is what stops lane_ceiling
    masking budget_full in the defer counts. The order below is load-bearing: reasons[0]
    is written to events.reason, which carries a 47-day series across this migration.
    """
    policy = state.policy
    guard_active = config.admission_mode == "reservation_plus_guard" and stats is not None

    reasons: list[str] = []
    if state.lanes >= policy.max_concurrent_lanes:
        reasons.append(LANE_CEILING)
    if job_class.needs_kvm and state.kvm:
        reasons.append(KVM_BUSY)
    if state.ram + job_class.ram_mb > policy.ram_budget_mb:
        reasons.append(BUDGET_FULL)
    disk_budget = policy.disk_budget_gb.get(job_class.work_disk)
    if disk_budget is not None and state.disk_gb[job_class.work_disk] + job_class.work_gb > (
        disk_budget
    ):
        reasons.append(DISK_FULL)
    if (
        guard_active
        and stats is not None
        and (
            stats.mem_available_mb < policy.host_free_ram_floor_mb
            or (policy.host_load_ceiling > 0 and stats.load_1m > policy.host_load_ceiling)
        )
    ):
        reasons.append(HOST_PRESSURE)
    return tuple(reasons)


def evaluate(
    jobs: list[QueuedJob],
    ledger: Ledger,
    config: ControllerConfig,
    host_stats: dict[str, HostStats] | None = None,
    healthy: set[str] | None = None,
) -> list[Decision]:
    """Decide admit/defer per job, accumulating admissions within the batch.

    Every enabled, healthy host whose `allowed_classes` admit the job is evaluated;
    the job is admitted to the passing host with the most free reservation headroom,
    ties broken by host name ascending so replays reproduce. When no host passes, the
    defer reports the gates of the *closest* host (fewest binding gates, then most
    headroom, then name) — with a single configured host that is bit-identical to the
    pre-migration output, which keeps the `reason`/`reasons` series comparable.

    `host_stats` is keyed by host name; a host with no entry has no live stats, so its
    host_pressure guard cannot fire. `healthy=None` means every configured host is healthy.
    """
    states: dict[str, _HostState] = {}
    for name, host in config.resolved_hosts().items():
        if not host.enabled:
            continue
        if healthy is not None and name not in healthy:
            continue
        policy = _policy(host, config)
        states[name] = _HostState(
            policy=policy,
            ram=ledger.total_ram(name),
            lanes=ledger.lane_count(name),
            kvm=ledger.kvm_in_use(name),
            disk_gb={disk: ledger.disk_gb_in_use(disk, name) for disk in policy.disk_budget_gb},
        )

    decisions: list[Decision] = []

    for job in jobs:
        if ledger.has_job(job.job_id):
            decisions.append(DeferDecision(job, (ALREADY_RUNNING,)))
            continue

        class_name = config.class_for(job.repo, job.labels)
        if class_name is None:
            decisions.append(DeferDecision(job, (NOT_ALLOWLISTED,)))
            continue

        job_class = config.classes[class_name]
        job_org = job.repo.split("/", 1)[0]
        eligible = [
            state
            for _, state in sorted(states.items())
            if class_name in state.policy.allowed_classes
            and (state.policy.allowed_orgs is None or job_org in state.policy.allowed_orgs)
        ]
        if not eligible:
            # Not a capacity verdict: no host would take this class however empty the fleet.
            decisions.append(DeferDecision(job, (NO_ELIGIBLE_HOST,), class_name=class_name))
            continue

        evaluated = [
            (state, _gates(state, job_class, config, (host_stats or {}).get(state.policy.name)))
            for state in eligible
        ]
        passing = [state for state, reasons in evaluated if not reasons]

        if not passing:
            closest, reasons = min(
                evaluated,
                key=lambda item: (len(item[1]), -item[0].headroom_mb, item[0].policy.name),
            )
            decisions.append(
                DeferDecision(job, reasons, host=closest.policy.name, class_name=class_name)
            )
            continue

        chosen = min(passing, key=lambda state: (-state.headroom_mb, state.policy.name))
        decisions.append(
            AdmitDecision(
                job=job,
                class_name=class_name,
                ram_mb=job_class.ram_mb,
                needs_kvm=job_class.needs_kvm,
                work_disk=job_class.work_disk,
                work_gb=job_class.work_gb,
                host=chosen.policy.name,
            )
        )
        chosen.ram += job_class.ram_mb
        chosen.lanes += 1
        chosen.kvm = chosen.kvm or job_class.needs_kvm
        if job_class.work_disk in chosen.disk_gb:
            chosen.disk_gb[job_class.work_disk] += job_class.work_gb

    return decisions
