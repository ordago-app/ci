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


@dataclass(frozen=True)
class QueuedJob:
    job_id: int
    repo: str
    labels: list[str] = field(default_factory=list)
    workflow: str = ""
    job_name: str = ""


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
    host: str = "powerserver"
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
    host: str = "powerserver"


@dataclass(frozen=True)
class DeferDecision:
    job: QueuedJob
    reasons: tuple[str, ...]
    # The host whose gates `reasons` describes: the closest host, i.e. the one that came
    # nearest to admitting. None only when no host was evaluated at all (already_running,
    # not_allowlisted, no_eligible_host) — those are not capacity verdicts and belong to
    # no host. Recording it is what makes ci_bench's per-host binding-gate table work;
    # with it always NULL that report section could never contain a real controller row.
    host: str | None = None
    # The class this job would run as. None only for already_running (the class belongs to
    # the lane, not to this decision) and not_allowlisted (no mapped label, so there is no
    # class to name). Every capacity defer carries it, which is what lets /status and the
    # `defer` event answer "which KIND of job is waiting" — without it a queue is a list of
    # opaque job ids, and six waiting node_heavy jobs read the same as six light ones that
    # an idle second host could have absorbed.
    class_name: str | None = None

    @property
    def reason(self) -> str:
        """Primary (first-binding) gate. Keeps parity with rows written before multi-gate."""
        return self.reasons[0]


Decision = AdmitDecision | DeferDecision
