from __future__ import annotations

from dataclasses import dataclass, field

NOT_ALLOWLISTED = "not_allowlisted"
ALREADY_RUNNING = "already_running"
LANE_CEILING = "lane_ceiling"
KVM_BUSY = "kvm_busy"
BUDGET_FULL = "budget_full"
DISK_FULL = "disk_full"
HOST_PRESSURE = "host_pressure"
# No configured host is enabled, healthy, and allowed to run the job's class. Distinct
# from a capacity gate: no amount of freed capacity would let this job in.
NO_ELIGIBLE_HOST = "no_eligible_host"
# The job's head was rejected by the automated review and some other job is waiting for a
# lane (`waits_for_lane`). Not a capacity verdict: the job would fit, but lanes go to heads
# still likely to merge first (docs/decisions/0032).
REVIEW_HELD = "review_held"

_LANE_PRESSURE_GATES = frozenset({BUDGET_FULL, DISK_FULL, LANE_CEILING})
# A defer binding one of these cannot be relieved by freeing a lane: freeing RAM or a lane
# slot does not free the KVM device.
_UNRELIEVABLE_GATES = frozenset({KVM_BUSY})


def waits_for_lane(reasons: tuple[str, ...]) -> bool:
    """True when freeing a lane's RAM, slot or disk could admit the deferred job.

    The one definition of "a job is waiting for a lane": the idle reaper spends warm
    lanes on it, and review-aware admission holds and cancels rejected heads' CI on it.
    `kvm_busy` and `host_pressure` alone do not count: no lane freed or withheld here
    would admit that job."""
    return bool(
        _LANE_PRESSURE_GATES.intersection(reasons)
    ) and not _UNRELIEVABLE_GATES.intersection(reasons)


# events.conclusion sentinel for a lane reaped because its HOST went away (ping()
# failed) while it still held a reservation — the desktop slept, or WSL shut down —
# AND the lane had already been attributed, so the row's job_id is a job we observed
# it running. Written without calling github.job_conclusion(): the job never reached a
# terminal outcome, so a lookup would be a wasted API call. Distinct from a genuine
# NULL conclusion (job finished, no terminal GitHub status observed) and from the
# "adopted"/"lookup_failed" sentinels controller.reconcile() already writes to the
# same column — ci_bench.py's infra_failures()/lookup_failures() depend on telling
# all of these apart.
#
# The attribution requirement is enforced at the WRITE site (controller.reconcile), not
# by a filter in ci_bench: it makes "every infra_failure row names a job that lane
# really ran" an invariant of the data rather than a rule each reader must remember.
INFRA_FAILURE = "infra_failure"
# events.conclusion sentinel for the same host-loss event on a lane that was never
# attributed — it was still booting, or idle, when its host vanished. The LANE was
# genuinely lost and that is worth surfacing, but there is no observed job to blame:
# job_id on such a row is spawned_for_job_id, a prediction GitHub frequently did not
# honour. Counting it as a job-level infra failure would import that guess and re-create
# the prediction-as-observation defect this whole change exists to remove, so it gets its
# own sentinel and its own counter (ci_bench.lanes_lost_unattributed) and never enters
# infra_failures().
LANE_LOST_UNATTRIBUTED = "lane_lost_unattributed"


# Per-head review states, as github-review's GET /heads names them.
REVIEW_PENDING = "pending"
REVIEW_RUNNING = "running"
REVIEW_APPROVED = "approved"
REVIEW_CHANGES_REQUESTED = "changes_requested"
REVIEW_FAILED = "failed"
REVIEW_SKIPPED = "skipped"

# A job whose `runs-on` carries `run-<its run id>` can only be taken by a runner
# registered with that label, i.e. a lane spawned for its own run. Without it GitHub
# hands the lane any queued job with the same labels, so admission order cannot bind.
RUN_LABEL_PREFIX = "run-"


@dataclass(frozen=True)
class QueuedJob:
    job_id: int
    repo: str
    labels: list[str] = field(default_factory=list)
    workflow: str = ""
    job_name: str = ""
    run_id: int | None = None
    head_sha: str | None = None
    # The head's state in github-review; None when github-review has no row for it
    # (no reviewer on this repo, an unlabelled PR, a branch push) or was unreachable.
    review: str | None = None

    @property
    def pinned(self) -> bool:
        return self.run_id is not None and f"{RUN_LABEL_PREFIX}{self.run_id}" in self.labels


@dataclass(frozen=True)
class RunningJob:
    """A job observed executing on a named runner. Labels are deliberately absent: the
    lane's class comes from its Docker labels, not from re-deriving it here."""

    job_id: int
    workflow: str
    job_name: str


@dataclass(frozen=True)
class Reservation:
    lane_id: str
    spawned_for_job_id: int
    repo: str
    class_name: str
    ram_mb: int
    needs_kvm: bool
    work_disk: str = "ssd"
    work_gb: int = 0
    workflow: str = ""
    job_name: str = ""
    host: str = ""  # empty means "not placed yet"; never a real machine
    # Observation state. GitHub hands a registering runner whichever queued job matches its
    # labels, so the job a lane runs is frequently not the one it was spawned for.
    # Epoch seconds the lane's container started, sourced from the daemon on every
    # reconcile rather than stamped at spawn — a controller restart must not reset the
    # age of a lane that never stopped running. None when the daemon gave no usable
    # timestamp; readers render "unknown" rather than a fabricated zero.
    started_at: float | None = None
    running_job_id: int | None = None
    # Set at spawn, cleared permanently on attribution. Runners are ephemeral (one job, then
    # exit), so a lane is idle only before its first job — there is no busy->idle transition.
    idle_since: float | None = None
    runner_id: int | None = None
    container_id: str | None = None
    # Why the idle reaper last declined to tear this lane down, and how many consecutive
    # ticks it has declined for. Every bail-out in _reap_idle_lane is a *deferral*, not a
    # decision -- it returns and retries next tick -- so without this a lane can sit past
    # idle_lane_max_seconds indefinitely while /status still calls it "booting". On
    # 2026-08-20 one held 7500 MB for 2153 s that way. Cleared on a successful reap.
    reap_blocked_reason: str | None = None
    reap_block_count: int = 0

    @property
    def claimed_job_id(self) -> int:
        """The job this lane is answerable for: observed if known, else the prediction."""
        return self.running_job_id if self.running_job_id is not None else self.spawned_for_job_id


@dataclass(frozen=True)
class AdmitDecision:
    job: QueuedJob
    class_name: str
    ram_mb: int
    needs_kvm: bool
    work_disk: str = "ssd"
    work_gb: int = 0
    host: str = ""  # empty means "not placed yet"; never a real machine


@dataclass(frozen=True)
class DeferDecision:
    job: QueuedJob
    reasons: tuple[str, ...]
    # The host whose gates `reasons` describes: the closest host, i.e. the one that came
    # nearest to admitting. None only when no host was evaluated at all (already_running,
    # not_allowlisted, no_eligible_host, review_held) — those are not capacity verdicts and
    # belong to no host. Recording it is what makes ci_bench's per-host binding-gate table work;
    # with it always NULL that report section could never contain a real controller row.
    host: str | None = None
    # The class this job would run as. None only for already_running (the class belongs to
    # the lane, not to this decision) and not_allowlisted (no mapped label, so there is no
    # class to name). Every capacity defer carries it, which is what lets /status and the
    # `defer` event answer "which KIND of job is waiting" — without it a queue is a list of
    # opaque job ids, and six waiting node_heavy jobs read the same as six light ones that
    # an idle second host could have absorbed.
    class_name: str | None = None
    # Every evaluated host's own binding gates, keyed by host name. `reasons` is only the
    # closest host's, and on its own it hid why a roomier host refused: 2026-11-07, node
    # jobs read budget_full (the elastic host was 42 MB short) while powerserver held them
    # on host_pressure. Empty for the same non-capacity defers that carry no `host`.
    gates_by_host: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        """Primary (first-binding) gate. Keeps parity with rows written before multi-gate."""
        return self.reasons[0]


Decision = AdmitDecision | DeferDecision
