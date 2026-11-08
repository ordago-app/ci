# A re-adopted lane that is running a job reads as idle

**Priority:** medium

## What was observed

2026-09-02, minutes after a `ci-controller` restart (the config-change handler
added in homelab ADR 0029), `make ci-status`:

```
STUCK: powerserver-cici-100348452164-653f01 (node, 5000 MB) idle 665s — deregistration_refused x14
```

That lane was not stuck and not idle. Its container was `Up 12 minutes` and its
job — ordago-apps `Lint + Unit (app · shared · functions)` — was `in_progress` on
runner `powerserver-cici-100348452164-653f01`, the lane's own name. The
reservation said:

```json
{"lane_id": "powerserver-cici-100348452164-653f01", "class_name": "node",
 "running_job_id": null, "idle_since": 1788369835.09,
 "reap_blocked_reason": "deregistration_refused"}
```

So the lane held its full 5000 MB reserve — 45% of the host budget, with 20 jobs
deferring on `budget_full` — while the reaper attempted to deregister it every
tick for eleven minutes.

## Why it happens

`_readopt` stamps `idle_since=_now()` on every lane the ledger does not recognise
after a restart, deliberately: the reaper needs an idle clock for a lane that
never takes a job. `idle_since` is cleared only when `reconcile()` resolves the
lane to a running job and sets `running_job_id` — and until that resolves the
lane looks, to `_reap_idle_lane` and to `ci-status`, exactly like a lane that
came up to an empty queue.

For this lane it never resolved: `reconcile()` kept taking the
`_note_unresolved_attribution` branch ("busy but not yet visible in the runs
list; retry next tick") even though the job was visible in the API the whole
time. Attribution for an adopted lane (`repo="(adopted)"`) has to search every
configured repo, and **why it failed here is the open question** — that is where
this should start.

## Why it is not already an incident

The reaper's targets are filtered on `running_job_id is None`, so it *selected*
this lane, and only GitHub's 422 on deregistering a busy runner stopped it. The
bail-out is deliberate and documented ("no evidence it is safe to destroy"), and
it held. But the safety of a live job currently rests on a remote API's refusal
rather than on local state, and the cost of the mislabel is a full reserve held
for the length of the job.

## Why it matters more now

Before homelab ADR 0029 a controller restart happened on an ordinary `make deploy`. It
now also happens on **every change to the pool config** (the consumer's `ci-controller.yml`), which is
the file capacity work edits — so the re-adoption path is exercised precisely
when someone is tuning the fleet and watching `ci-status` to judge the result.
A lane wrongly reading STUCK is a misleading signal at exactly the wrong moment.

## Where to look

- `_readopt` / `reconcile()` attribution in `services/ci-controller/src/controller.py`
  (the `_note_unresolved_attribution` branch, and what `lookup.complete` means for
  an `(adopted)` lane).
- `_reap_idle_lane`'s target filter, and whether "the daemon says this container is
  running a job" should outrank a stamped `idle_since`.
- `ci-status`'s STUCK rendering: a lane whose container is running should never
  render as stuck, whatever the ledger thinks.

Moved from homelab on 2026-11-08; the code it names is this repo's now.

Related: [`docs/incidents/2026-08-20-powerserver-double-crash.md`](../../incidents/2026-08-20-powerserver-double-crash.md) (blocked reaps holding reserve),
[ADR 0020](../../decisions/0020-node-class-split-by-measured-per-job-footprint.md)
(class-aware re-adoption), homelab ADR 0029 (a lane host takes every class it can afford).
