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
# failed) while it still held a reservation — the desktop slept, or WSL shut down.
# Written without calling github.job_conclusion(): the job never reached a terminal
# outcome, so a lookup would be a wasted API call. Distinct from a genuine NULL
# conclusion (job finished, no terminal GitHub status observed) and from the
# "adopted"/"lookup_failed" sentinels controller.reconcile() already writes to the
# same column — ci_bench.py's infra_failures()/lookup_failures() depend on telling
# all of these apart.
INFRA_FAILURE = "infra_failure"


@dataclass(frozen=True)
class QueuedJob:
    job_id: int
    repo: str
    labels: list[str] = field(default_factory=list)
    workflow: str = ""
    job_name: str = ""


@dataclass(frozen=True)
class Reservation:
    lane_id: str
    job_id: int
    repo: str
    class_name: str
    ram_mb: int
    needs_kvm: bool
    work_disk: str = "ssd"
    work_gb: int = 0
    workflow: str = ""
    job_name: str = ""
    host: str = "powerserver"


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

    @property
    def reason(self) -> str:
        """Primary (first-binding) gate. Keeps parity with rows written before multi-gate."""
        return self.reasons[0]


Decision = AdmitDecision | DeferDecision
