# 32. Review-aware admission binds through a per-run runner label

Date: 2026-10-17

## Status

Accepted. Adds a review tier ahead of the largest-first order of
[ADR 0027](0027-largest-first-admission.md), and brings review containers into the
capacity model of [ADR 0015](0015-ci-capacity-model-and-second-host.md). Phase 3 of the
cross-repo plan `review-ci-scheduling` (`_workspace/docs/plans/`).

## Context

The automated review is fast and usually says no: over 30 days on ordago-apps, 68 % of
first-round verdicts were `REQUEST_CHANGES`, at a p50 of 1.6 min. CI is slow (20–30 lane
minutes per head) and usually passes. Most CI spent before a PR's first approval is
spent on heads that will not merge. Once github-review publishes each head's state
(`GET /heads`), the controller could give scarce lanes to heads likely to merge first.

That only works if the controller decides **which job** a lane runs, and it does not.
A lane is an ephemeral runner registered with the admitted job's labels; GitHub hands it
whichever queued job those labels satisfy, and documents no ordering. ADR 0027 already
rejected drain mode on these grounds. Measured from `metrics.db` `attach` events, 30 days
to 2026-10-17:

| repo / class | attributed lanes | ran another job than spawned for |
|---|---:|---:|
| ordago-apps `light` | 3520 | 2199 (62 %) |
| ordago-apps `node_small` | 3116 | 1781 (57 %) |
| ordago-apps `node` | 1384 | 461 (33 %) |
| homelab `light` | 534 | 383 (72 %) |

Of the last 40 such swaps, 11 ran a job from a **different head**. Ordering admission by
review state without a fix would rank jobs and then let GitHub hand the lane to whichever
head it liked.

Mechanisms looked at:

- **JIT runner config** (`generate-jitconfig`): still a runner with labels and a group.
  GitHub matches it to jobs by labels exactly as a registration-token runner. No job id.
- **Runner groups**: org-level only (the homelab repo is on a personal account), and they
  restrict by repository and workflow file, not by run or head.
- **A per-run label in `runs-on`**: `runs-on` accepts the `github` context, so a job can
  require `run-${{ github.run_id }}`. A runner must carry *every* label a job asks for,
  and the controller already registers each lane with the admitted job's full label set.
  A lane spawned for run R then can only take jobs of run R with the same class labels.
  Every job in a run shares one head, so admission order binds at head granularity, which
  is the granularity review state has.

## Decision

1. **Jobs opt in with `run-${{ github.run_id }}` in `runs-on`.** The controller needs no
   spawn change: the label is already in the job's labels, so it reaches `RUNNER_LABELS`.
   `class_for` ignores it (it is in no `label_class`). homelab's workflows carry it, and a
   repo-level test keeps every self-hosted homelab job pinned. A job without the pin is
   **not tiered**: ordering it would be a guess, so it keeps the largest-first order.

2. **Admission ranks pinned jobs by their head's review state**, then largest-first
   within a rank:

   | rank | head state | rule |
   |---|---|---|
   | 0 | `approved`, no row (no reviewer, unlabelled PR, branch push), `failed`, `skipped`, or unpinned | first |
   | 1 | `pending` / `running`, class in `review_admission.cheap_classes` | speculative, never idle |
   | 2 | `pending` / `running`, any other class | after the cheap ones |
   | 3 | `changes_requested` | deferred `review_held` once any job above it waits for a lane |

   Review state never starves work it has nothing to say about, which is why every
   "no verdict will come" state ranks with approved. "Waits for a lane" is the idle
   reaper's definition (`models.waits_for_lane`): deferred on budget, disk or the lane
   ceiling, and not on `kvm_busy`. A job held only by `kvm_busy` or `host_pressure` waits
   for nothing a rejected head's lane takes, and cancelling cannot be undone.

3. **Rejected heads' queued runs are cancelled only under pressure.** When some
   non-rejected job waits for a lane, the controller cancels each rejected head's
   run. GitHub cancels runs, not jobs, so a run is cancelled only when it is `queued` AND
   none of its jobs is `in_progress` (every job is read: GitHub can report a run `queued`
   while a job of it runs). A run the controller holds a lane for, admitted this tick or
   earlier, is left alone, because a booting runner could start a job between the check
   and the cancel. Without contention rejected CI runs, so the author fixes CI failures
   and findings in one round. Each cancelled job is a `cancel` event in `metrics.db`.

4. **github-review unreachable degrades to today's order, loudly.** One 3 s attempt per
   tick; on failure every job admits as unreviewed, the outage is logged at ERROR (first
   tick, then every 60th) and `/status` shows `review_state_reachable: false`. The URL is
   required config (`GITHUB_REVIEW_URL`), never defaulted.

5. **Review containers are charged to the ledger by counting them.** Each tick the
   controller lists `claude-review-<job>-…` / `codex-review-<job>` containers on
   `review_admission.session_host` through the docker-socket-proxy it already holds, and
   the scheduler charges `session_ram_mb` (600, measured) each against that host's RAM
   budget before admitting. Counting containers, not github-review's `running` rows: the
   container is what holds the RAM, the count survives github-review being down, and a
   stale `running` row (three were 45–53 days old on 2026-10-17) cannot reserve phantom
   memory. The name is the contract; a repo-level test reads both services' sources to
   hold them to it. Reviews do not count against `max_concurrent_lanes`: they use ~2 % CPU.

## Rejected

- **Ordering unpinned jobs by review state anyway.** It would pass every unit test and
  bind only when GitHub happened to agree.
- **Reading github-review's running count for the charge.** Depends on the store being
  clean and on github-review answering; the container list depends on neither.
- **A `review` job class.** It would enter every host's default `allowed_classes` and the
  label maps while no job could ever carry it.

## Consequences

- A pinned lane can only serve its own run. A lane whose job was cancelled during boot
  can no longer pick up a sibling run's job; it idles until the idle reaper takes it
  (`idle_grace_seconds` under pressure, `idle_lane_max_seconds` always).
- Pinning also ends the attribution trap for pinned repos: `spawned_for_job_id` and the
  job actually run share a run.
- **ordago-apps and cultuvilla are untiered until their workflows add the pin** (one
  PR each, in their repos). Until then their jobs keep largest-first order; cancellation
  of rejected queued runs already applies to them.
- Cancelling needs the runner App to hold **Actions: Read and write** on each repo. Without
  it every cancel logs one ERROR per run and nothing else changes.
- `review_held` is a new defer reason in `metrics.db`; `make ci-report`'s per-reason
  tables show it as its own row.

## Amendment (2026-11-08)

Decision 5's contract is now the label, not the name. The reviewer had renamed its
containers `review-<instance>-<account>-<job>` and labels each with
`ordago.ci.github-review.instance`, so the name pattern above matched none of them and
no review was charged to the ledger. The controller counts running containers carrying
that label, whatever instance set it, and
`services/ci-controller/tests/test_review_contract.py` holds both sides to the one name.
