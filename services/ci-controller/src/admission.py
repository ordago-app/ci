from __future__ import annotations

from dataclasses import dataclass, replace

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
    REVIEW_CHANGES_REQUESTED,
    REVIEW_HELD,
    REVIEW_PENDING,
    REVIEW_RUNNING,
    AdmitDecision,
    Decision,
    DeferDecision,
    QueuedJob,
    waits_for_lane,
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
    placement_rank: int


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
        placement_rank=host.placement_rank,
    )


def ram_budget_mb(host: HostConfig, config: ControllerConfig, ram_offers: dict[str, int]) -> int:
    """The RAM a host may reserve this tick: its configured ceiling, lowered to its own
    offer when it publishes one. An elastic host with no reading this tick gets 0 --
    never the ceiling, which would over-admit onto a guest the balloon has shrunk."""
    ceiling = config.ram_budget_mb if host.ram_budget_mb is None else host.ram_budget_mb
    if host.ram_offer_reserve_mb is None:
        return ceiling
    return min(ceiling, ram_offers.get(host.name, 0))


# Load average counts tasks blocked in uninterruptible IO as well as runnable ones, so
# on a box whose root is a spinning disk one swapping container lifts it far past the
# ceiling while every CPU idles: 2026-10-20, load 20-50 at 5-8 % CPU for five hours, and
# every CI job deferred host_pressure (docs/incidents/2026-10-20-ci-held-by-io-wait-load.md).
# The ceiling is there to stop CPU oversubscription (2026-08-07), so load only counts
# while the CPUs are actually contended. At 2:1 oversubscription some task is always
# waiting, i.e. PSI cpu `some` near 100 %; half of that still trips well before it.
_LOAD_COUNTS_FROM_CPU_PRESSURE_PCT = 50.0


def _cpu_oversubscribed(stats: HostStats, load_ceiling: float) -> bool:
    if load_ceiling <= 0 or stats.load_1m <= load_ceiling:
        return False
    # No PSI reading: the plain load-average gate, as before, rather than no gate.
    return (
        stats.cpu_pressure_pct is None
        or stats.cpu_pressure_pct >= _LOAD_COUNTS_FROM_CPU_PRESSURE_PCT
    )


def host_pressure_binds(
    stats: HostStats | None,
    config: ControllerConfig,
    *,
    free_ram_floor_mb: int,
    load_ceiling: float,
) -> bool:
    """Whether the live-stats guard holds this host. Public so /status reports the very
    verdict admission used instead of a reader re-deriving it."""
    if config.admission_mode != "reservation_plus_guard" or stats is None:
        return False
    return stats.mem_available_mb < free_ram_floor_mb or _cpu_oversubscribed(stats, load_ceiling)


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
    if host_pressure_binds(
        stats,
        config,
        free_ram_floor_mb=policy.host_free_ram_floor_mb,
        load_ceiling=policy.host_load_ceiling,
    ):
        reasons.append(HOST_PRESSURE)
    return tuple(reasons)


_REJECTED_RANK = 3


def _review_rank(job: QueuedJob, class_name: str | None, config: ControllerConfig) -> int:
    """Where a job's head stands in the review tiers, lowest admitted first.

    0  approved, or no review that can bind: an unpinned job (GitHub would hand the lane
       to any job with its labels, so the order would be a guess), a repo or PR with no
       reviewer, a head whose review failed or was skipped. Review state must never
       starve work it has nothing to say about.
    1  under review, cheap class.
    2  under review, heavy class.
    3  changes requested: admitted only when nothing ranked above it waits.
    """
    if not job.pinned:
        return 0
    if job.review in (REVIEW_PENDING, REVIEW_RUNNING):
        return 1 if class_name in config.review_admission.cheap_classes else 2
    if job.review == REVIEW_CHANGES_REQUESTED:
        return _REJECTED_RANK
    return 0


def _admission_order(
    jobs: list[QueuedJob], config: ControllerConfig
) -> list[tuple[QueuedJob, str | None, int]]:
    """Pair every job with its class and review rank, ordered by rank, then by reserve,
    biggest first.

    Admission below is greedy over this order, so a job is offered the machine ahead of
    every job behind it. Within a rank, largest-first: the order jobs arrive in is
    GitHub's, and under it a stream of small lanes can hold `node_heavy` -- 78% of the
    budget, so it fits only in a near-empty machine -- out of capacity it would have
    fitted in. That fragmentation is what puts heavy's p90 queue wait at 3989 s against
    node_small's 782 s (docs/ci-capacity-report.md, 2026-08-21).

    `sorted` is stable, so jobs of equal rank and reserve keep arrival order and replays
    stay reproducible. A job whose labels map to no class is deferred `not_allowlisted`
    without touching any budget, so its position cannot change another job's verdict.
    """
    ranked = []
    for job in jobs:
        class_name = config.class_for(job.repo, job.labels)
        ranked.append((job, class_name, _review_rank(job, class_name, config)))
    return sorted(
        ranked,
        key=lambda item: (
            item[2],
            -(config.classes[item[1]].ram_mb if item[1] is not None else 0),
        ),
    )


def evaluate(
    jobs: list[QueuedJob],
    ledger: Ledger,
    config: ControllerConfig,
    host_stats: dict[str, HostStats] | None = None,
    healthy: set[str] | None = None,
    review_sessions: int = 0,
    ram_offers: dict[str, int] | None = None,
) -> list[Decision]:
    """Decide admit/defer per job, accumulating admissions within the batch.

    Jobs are considered in review rank, then largest reserve first (see
    `_admission_order`), not in arrival order, so the returned decisions come back in
    that order too. A rejected head (rank 3) is deferred `review_held` once any job
    ranked above it has deferred waiting for a lane (`waits_for_lane`).

    `review_sessions` is how many review containers are running on
    `review_admission.session_host`; each is charged `session_ram_mb` against that
    host's budget before any job is considered.

    Every enabled, healthy host whose `allowed_classes` admit the job is evaluated;
    the job is admitted to the passing host with the lowest `placement_rank`, then the
    most free reservation headroom, ties broken by host name ascending so replays
    reproduce. Rank only orders hosts that already pass, so it never defers a job.
    When no host passes, the defer reports the gates of the *closest* host (fewest
    binding gates, then most headroom, then name) — with a single configured host that
    is bit-identical to the pre-migration output, which keeps the `reason`/`reasons`
    series comparable.

    `host_stats` is keyed by host name; a host with no entry has no live stats, so its
    host_pressure guard cannot fire. `healthy=None` means every configured host is healthy.
    `ram_offers` is keyed the same way; see `ram_budget_mb`.
    """
    states: dict[str, _HostState] = {}
    for name, host in config.resolved_hosts().items():
        if not host.enabled:
            continue
        if healthy is not None and name not in healthy:
            continue
        policy = replace(
            _policy(host, config), ram_budget_mb=ram_budget_mb(host, config, ram_offers or {})
        )
        states[name] = _HostState(
            policy=policy,
            ram=ledger.total_ram(name),
            lanes=ledger.lane_count(name),
            kvm=ledger.kvm_in_use(name),
            disk_gb={disk: ledger.disk_gb_in_use(disk, name) for disk in policy.disk_budget_gb},
        )
    session_host = states.get(config.review_admission.session_host)
    if session_host is not None:
        session_host.ram += review_sessions * config.review_admission.session_ram_mb

    decisions: list[Decision] = []
    # Whether any job ranked above a rejected head has deferred waiting for a lane.
    waiting = False

    for job, class_name, rank in _admission_order(jobs, config):
        decision = _decide(job, class_name, rank, waiting, ledger, config, states, host_stats)
        decisions.append(decision)
        if (
            rank < _REJECTED_RANK
            and isinstance(decision, DeferDecision)
            and waits_for_lane(decision.reasons)
        ):
            waiting = True

    return decisions


def _decide(
    job: QueuedJob,
    class_name: str | None,
    rank: int,
    waiting: bool,
    ledger: Ledger,
    config: ControllerConfig,
    states: dict[str, _HostState],
    host_stats: dict[str, HostStats] | None,
) -> Decision:
    """One job's verdict. An admission is charged to its host's state in place, so the
    next job in the batch sees the capacity this one took."""
    if ledger.has_job(job.job_id):
        return DeferDecision(job, (ALREADY_RUNNING,))

    if class_name is None:
        return DeferDecision(job, (NOT_ALLOWLISTED,))

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
        return DeferDecision(job, (NO_ELIGIBLE_HOST,), class_name=class_name)

    if rank == _REJECTED_RANK and waiting:
        return DeferDecision(job, (REVIEW_HELD,), class_name=class_name)

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
        return DeferDecision(
            job,
            reasons,
            host=closest.policy.name,
            class_name=class_name,
            gates_by_host={state.policy.name: gates for state, gates in evaluated},
        )

    chosen = min(
        passing,
        key=lambda state: (state.policy.placement_rank, -state.headroom_mb, state.policy.name),
    )
    chosen.ram += job_class.ram_mb
    chosen.lanes += 1
    chosen.kvm = chosen.kvm or job_class.needs_kvm
    if job_class.work_disk in chosen.disk_gb:
        chosen.disk_gb[job_class.work_disk] += job_class.work_gb
    return AdmitDecision(
        job=job,
        class_name=class_name,
        ram_mb=job_class.ram_mb,
        needs_kvm=job_class.needs_kvm,
        work_disk=job_class.work_disk,
        work_gb=job_class.work_gb,
        host=chosen.policy.name,
    )
