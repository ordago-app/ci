# 18. A CI lane is attributed to the job it runs, and idle lanes are reclaimed on demand

Date: 2026-08-09

## Status

Accepted. Extends [ADR 0015](0015-ci-capacity-model-and-second-host.md)'s measurement
model and [ADR 0016](0016-opportunistic-second-ci-host.md)'s multi-host scheduler; neither
is superseded. Implemented in #125.

## Context

The controller's ledger recorded, for each lane, the job the lane was **spawned for**.
GitHub hands a registering self-hosted runner whichever queued job matches its label set —
not necessarily the one that motivated the spawn — and nothing ever reconciled the two. The
prediction was therefore wrong whenever more than one job of a class was queued, which is
the normal case during a burst.

Nothing downstream knew that. `/status` named the wrong job per lane; the `already_running`
gate held a claim on a job the lane never ran, so that job kept deferring until the lane
exited; and every reap metric was tagged with the wrong id. ADR 0015 then added
`conclusion` lookups keyed on that same id, so `ci_bench.infra_failures()` counted
still-queued jobs' NULL conclusions as infra failures that never happened.

The evidence that this mattered is in the repo's own history: the `node` class split
(#109) was calibrated by joining reap `lane_id` to the GitHub job whose `runner_name`
matched it — **by hand**, because the recorded `job_id` could not be trusted. The
calibration was sound; the instrument was not.

A second, independent leak shared the same root cause. A lane takes 20–30 s to boot. If the
job that motivated it is cancelled inside that window, the lane comes up to an empty queue
and holds its full reserve — and one of a small number of lane slots — until some later job
happens to match its labels. Both defects reduce to the controller never asking GitHub what
a lane is actually doing.

## Decision

**1. Attribution is observed, not predicted.** A reservation carries `spawned_for_job_id`
(the admission-time prediction, immutable) and `running_job_id` (`None` until observed).
`claimed_job_id` is the observation when present and the prediction otherwise, and the
admission gate reads it — so a lane going busy with another job *releases* the claim on its
spawned-for job, which is what unblocks the starved job.

**2. Polling, not webhooks, and asymmetric.** Webhooks are unavailable: the whole chain is
outbound and there is no public ingress. Busy-state is cheap (one runner listing per repo
per tick) and is polled every tick; resolving *which* job a busy lane took costs 1+N calls
and is spent only on the idle→busy edge. Runners are ephemeral — one job, then exit — so
that edge occurs at most once per lane, making attribution cost O(lanes started) rather
than O(ticks). This is what keeps the design inside the installation token's hourly budget.

**3. GitHub, not the controller, decides whether a lane is safe to destroy.**
`DELETE /repos/{repo}/actions/runners/{id}` returns 422 for a busy runner. The reaper
deregisters first and tears the container down only if GitHub agreed, which answers "was
this lane handed a job since our last poll?" atomically rather than from a poll that is up
to a tick stale.

**4. Absence must be positively confirmed.** For a lane with no registration to point at,
"we never heard about it" never authorises a teardown — only "GitHub was asked and says it
does not exist" does. A runner listing that failed, was truncated, or was never attempted
for that repo yields no such evidence, and the lane stands. Every degradation therefore
biases toward keeping a lane alive.

**5. Lane identity is not derived from job identity.** A lane id carries a per-spawn
suffix. Without it, decision 1's claim release is inert: the freed job is re-admitted onto
a lane id that already exists, colliding every tick while the job starves.

**6. Reclamation is demand-driven.** An idle lane is warm capacity, so it is reaped early
only when that tick actually deferred someone on a gate that reaping can relieve, and never
more lanes than there was demand. `kvm_busy` is excluded: freeing a lane's RAM does not free
the KVM device, so reaping against it destroys warm lanes while the queue stays stuck. A
grace window protects a booting lane, which is indistinguishable from an idle one from
outside, and an absolute ceiling ensures nothing leaks forever.

**7. Attribution is recorded per row and never backfilled.** An `attributed` column marks
rows whose job identity was observed; it is NULL on every row written before this shipped.
Analyses that need per-job truth filter on it explicitly, so a prediction can never be read
as an observation. Class-level percentiles span the boundary unaffected, which is why the
existing reserve tuning survived.

**8. An outcome is attributed to a job only if the row names a job we observed.** A lost
host is an observed fact about the *host*; it does not make the *row's* job id true. So a
lane lost with its host is recorded as a job-level `infra_failure` only when it was
attributed, and as a lane-level loss otherwise. This is enforced where the row is
**written**, not filtered where it is read, so the invariant cannot be broken later by a
reader who forgets a predicate.

## Consequences

`make ci-report` can answer per-job questions for the first time — the join #109 did by
hand is now a property of the data. Spawn-vs-run divergence and idle-lane reclamation are
both counted, so the cost this ADR addresses is visible rather than inferred.

Two reported numbers move on deploy, both deliberately. `lookup_failures()` falls, because
an unattributed reap now writes a sentinel instead of querying the conclusion of a job the
lane never ran. `infra_failures()` becomes a confirmed-failure count with a documented
undercount rather than reporting "unavailable": an unattributed NULL is not counted, but it
no longer suppresses the sentinels beside it.

Decision 8 cannot reach rows written before it. One `infra_failure` row recorded on
2026-08-09 predates attribution and permanently overstates the job-level count by one until
reclassified by hand.

Decision 4 makes the reaper strictly less aggressive as reliability degrades. A prolonged
GitHub or host outage means idle reserves are held rather than reclaimed. That is the
intended trade: the alternative failure mode is destroying containers running real jobs.

Attribution depends on `runner_name` equalling the lane id, and on the runners and
workflow-jobs endpoints. Both listings are bounded rather than fully paginated, and a
truncated scan is reported as truncated rather than as an empty result — conflating the two
is what hid a missing-pagination defect through several review rounds.
