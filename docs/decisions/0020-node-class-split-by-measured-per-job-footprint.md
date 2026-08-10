# 20. Splitting the node class by measured per-job footprint

Date: 2026-08-09

## Status

Accepted. Extends [ADR 0015](0015-ci-capacity-model-and-second-host.md), which
identified `node`'s bimodality as a class-granularity defect and deferred the
split until the underlying cause was identified.

**Renumbered from 0016 to 0020 on 2026-08-10.** It was authored the day after
[ADR 0016](0016-opportunistic-second-ci-host.md) and took the same number. 0016
keeps it as the earlier and far more referenced of the two; this ADR had no
inbound references, so the renumber breaks no links. Content is unchanged.

## Context

`node` showed p50 ≈ 1.27 GB against p90 ≈ 5.9 GB — two populations under one
label. ADR 0015 recorded the correct response (split the class) and noted that
Task 3 had started capturing `workflow` and `job_name` on every queued job "for
exactly this purpose".

That capture is necessary but not sufficient, because of how lanes bind to jobs.

### The attribution trap

A lane is not bound to the job it was admitted for. The controller registers an
ephemeral runner with the admitted job's `runs-on` labels; GitHub then hands that
runner **whichever queued job those labels satisfy**, which is frequently a
different job. Confirmed in production: container `cici-93133720215` executed job
`93122957644`, and GitHub's record for that job listed
`runner_name = powerserver-cici-93133720215`.

`Controller.reconcile()` emits the reap event with `job_name=res.job_name` — the
name of the job the lane was *spawned for*. So a per-job peak table built from
reap rows attributes each lane's measured footprint to the wrong job whenever the
lane was reassigned. Building the split on that table would have produced the
wrong tiers.

The reap event does carry `lane_id`, and GitHub's job records carry
`runner_name`, and those are the same string. Joining on it recovers true
attribution without needing the controller to change.

### Method

Reap events since 2026-08-05 (page-cache-corrected `peak_ram_mb` only) joined to
`ordago-apps` job records by `lane_id == runner_name`.

One correction is required. Duplicate admissions reuse a lane id — the lane id is
derived from the spawned-for job id, and a job whose lane was reassigned stays
queued and gets admitted again once that lane exits. **18.3% of runner names (88
of 482) mapped to more than one job name** and were discarded as unattributable.
Skipping that step inflates the light-looking jobs with other jobs' peaks: before
dropping them, `Expo dep check` appeared to reach 7049 MB; after, its measured max
is 1238 MB.

## Decision

**1. `node` splits three ways by measured per-job peak RAM.** From 214 cleanly
attributed reaps:

| job | n | p50 | p95 | max | share of node lanes | tier |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| E2E · Firestore emulator (shared) | 17 | 6086 | 7187 | 7187 | 20% | `node_heavy` 7500 |
| Lint + Unit (app · shared · functions) | 22 | 3847 | 5059 | 5372 | 26% | `node` 5000 |
| Expo dep check | 24 | 822 | 1231 | 1238 | 28% | `node_small` 1800 |
| Unit · Vitest (functions) | 12 | 1239 | 1403 | 1403 | 14% | `node_small` 1800 |
| Next.js build + Vercel deploy | 9 | 1271 | 1402 | 1402 | 10% | `node_small` 1800 |

**2. `node` stays at 5000.** Removing the emulator suite from the class does not
lower what the remaining jobs need: `Lint + Unit` alone measures p95 5059. The
split is not a way to reduce this reserve, and the expectation that it would be
is the main thing this ADR exists to correct.

**3. The throughput win is `node_small`, not `node_heavy`.** Three jobs totalling
52% of node lanes all fit within 1403 MB while being charged 5000. `node_heavy`
is a *safety* correction in the opposite direction — the emulator suite's median
(6086) already exceeded its reserve and 14 of 17 runs blew through it, so on its
own that tier reduces concurrency. Taken together the tiers free budget on half
the traffic and stop under-pricing the fifth.

**4. Re-adopted lanes keep their class.** A controller restart re-adopted every
running lane at `default_class`, the *cheapest* class. Measured twice: reaps
tagged `class=light repo=(adopted)` with 7084 MB and 7100 MB peaks against a
700 MB reservation — a 10x under-reserve that hands the difference to new
admissions. The lane's class is now stamped on the container
(`com.homelab.ci-controller.class`) and restored on re-adopt. An unrecognised or
absent class label reserves the **largest** configured class and logs a warning,
following ADR 0015's asymmetry: over-reserving costs deferrals, which are free;
under-reserving is the OOM path.

## Consequences

- **The label change must land second.** `class_for` falls through to
  `default_class` for any unmapped `self-hosted` label, and `default_class` is
  the cheapest class. If `ordago-apps` starts emitting `ordago-ci-heavy` before
  this config is deployed, the emulator suite books 700 MB while using 7 GB —
  strictly worse than today. Order: deploy homelab config, then change
  `runs-on` in `ordago-apps`.

- **A job must carry exactly one tier label.** `class_for` returns the first
  mapped label in the job's own `runs-on` order, so a job carrying both
  `ordago-ci` and `ordago-ci-small` gets whichever it happens to list first.
  `test_operator_config_never_underprices_a_mapped_label` pins every mapping.

- **Per-job numbers in the generated report remain unreliable** until reap events
  record the job the lane actually ran. `make ci-report` is class-granularity and
  stays trustworthy; any per-job breakdown derived from reap `job_name` is not.
  Re-run the join above rather than trusting it.

- **`n` is 9–24 per job over four days.** Small, but the maxima cluster tightly
  (1238 / 1402 / 1403 for the three `node_small` jobs), which is what the tier
  boundary rests on. Revisit after a fuller window.

- **`ordago-apps#564` folds the functions suite into the emulator lane**, removing
  one `node_small` job and raising what `node_heavy` must cover. The suites run
  sequentially so the lane peak is `max()`, not the sum; 7500 covers
  `max(7187, 1403)` with headroom.
