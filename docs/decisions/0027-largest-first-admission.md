# 27. Admitting the largest queued job first

Date: 2026-09-02

## Status

Accepted. Refines the admission loop introduced with the capacity model in
[ADR 0015](0015-ci-capacity-model-and-second-host.md) and the class tiers of
[ADR 0020](0020-node-class-split-by-measured-per-job-footprint.md).

## Context

`admission.evaluate()` walked the queue in the order GitHub returned it and
admitted greedily: for each job in turn, take the first host whose gates all
pass, charge its reserve, move on. Arrival order was never a decision — it was
whatever `GET /actions/runs` plus a per-run jobs call happened to produce.

That order is not neutral, because the classes differ by more than an order of
magnitude: `light` reserves 700 MB and `node_heavy` 8600 MB against an 11000 MB
budget. A big job only fits in a nearly empty machine, so every small job walked
before it can take the space it needed. The queue-wait percentiles in
`docs/ci-capacity-report.md` (window ending 2026-08-21) show the resulting split:

| class | p50 | p90 | p95 |
|---|---:|---:|---:|
| `node_small` | 152 s | 782 s | 989 s |
| `node` | 256 s | 2003 s | 3880 s |
| `node_heavy` | 934 s | 3989 s | 5192 s |
| `emulator` | 1167 s | 5183 s | 6379 s |

Some of that spread is irreducible — a job needing 78% of the box waits for a
window that rarely exists. The part that is not irreducible is fragmentation:
capacity that a big job could have used, spent on small jobs that would also have
fitted around it later.

## Decision

Order the queue by reserve, largest first, before the greedy loop
(`admission._largest_first`). This is first-fit-decreasing bin packing; it does
not change any gate, any reserve, or any host-placement rule, only which job is
offered the machine first.

`sorted` is stable, so jobs of equal size keep arrival order and a replay of the
same queue reproduces the same decisions. A job whose labels map to no class
reserves nothing and is deferred `not_allowlisted`, so its position cannot change
another job's verdict.

The decisions list now comes back in that order rather than in arrival order.
Nothing downstream depended on it: `Controller.tick` emits per-decision events,
and `_track_queue_age` / `_reap_idle_lanes` both aggregate.

## Rejected: reserving capacity for a starved job (drain mode)

The obvious companion is to stop a big job being starved outright — after N
deferrals on `budget_full`, withhold admissions until it fits. Rejected on three
grounds:

1. **There is no starvation to cure.** Big classes wait longer, but they land;
   the tail is fragmentation, and largest-first is the direct fix for that. A
   fairness mechanism solves a problem the data does not show.
2. **It cannot be enforced.** A lane registers with the admitted job's labels and
   GitHub hands it *whichever* queued job those labels satisfy — 959 of 1886
   attributed lanes ran a job other than the one they were spawned for
   ([ADR 0020](0020-node-class-split-by-measured-per-job-footprint.md), "the
   attribution trap"). Held capacity would be held for a class, not a job, and
   whichever job GitHub picked would consume it.
3. **It spends the scarcest resource to buy latency.** Draining deliberately
   idles RAM on a host whose 16 GB cannot be extended
   ([ADR 0015](0015-ci-capacity-model-and-second-host.md)), and adds a threshold
   to tune.

Revisit only if `make ci-report` shows the heavy tail unmoved after this change —
that would mean the wait is contention, not packing, and the answer there is
[a smaller heavy job](0015-ci-capacity-model-and-second-host.md), not a fairer
queue.

## Consequences

- Over-reserving a job's tier label now also jumps it up the queue, on top of
  committing budget nothing uses. "Take the cheapest tier your job fits in" is
  unchanged as guidance but now has a second failure mode behind it.
- Per-class queue-wait percentiles are the measurement for this change; the next
  `make ci-report` after deploy is the verification, not the test suite.
