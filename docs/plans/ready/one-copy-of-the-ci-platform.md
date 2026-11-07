# One copy of the CI platform

**Priority:** high

Decided 2026-11-07 by the first operator: there is one copy of the platform, this one.
Each consumer keeps only its configuration and deploys the `ordago.ci` roles pinned by
SHA.

## Why there are two copies today

The platform was extracted from the first operator's private repo on 2026-08-26 with
history preserved. The extraction plan's last step was the cut-over: the consumer
switches its plays to the `ordago.ci` roles and deletes its own `services/` trees.
That step only ever happened for `ci_lane_host`. Everything else is still deployed
from the consumer's own copy, built in place by its `services.yml`, and the SHA pin
in its `ansible/requirements.yml` governs only the collection.

So AGENTS.md invariant 5 ("a commit here reaches nobody's hardware until that
machine's owner deploys it; the pin *is* the boundary") holds for the lane host and
for nothing else. A change landed here does not ship, and a change landed there never
reaches this repo. Nothing fails when the copies diverge; the only symptom is a pool
that behaves unlike the code you are reading.

## How far apart they are (2026-11-07)

| Tree | Deployed from | Files that differ | Ahead |
|---|---|---|---|
| `services/ci-controller` | the consumer's copy | 29 (733 changed lines in `src/` alone) | the consumer's copy |
| `services/github-actions-runner` | the consumer's copy | 7 | unchecked |
| `services/github-review` | the consumer's copy | 40 | this repo (reconciled 2026-11-06) |
| `services/ci-lane-host` | **this repo**, `ordago.ci.ci_lane_host` | 3 | n/a: the consumer's copy is dead |

For `ci-controller` the consumer's copy is close to a superset. About 25 commits
landed there after the extraction and never came here: review-aware admission
(`review_client.py`, the review tiers, the per-run runner pin), largest-reserve-first
admission, elastic hosts budgeted by their guest's RAM offer, overflow placement by
`placement_rank`, and on 2026-11-07 the per-host reason every queued job waits plus
the live host pressure in `/status`. The fixes that went the other way (#8, #10, #16,
#17) were each ported by hand. This repo's own extra piece is the `ci_controller`
role and its tests.

## Tasks

Each task is one PR. The pool keeps running throughout. Each consumer-side step
is `--check` first and is deployed only by that machine's owner.

1. **Make this repo's `ci-controller` the superset.** Take the consumer's
   `services/ci-controller/` as the base, as #25 did for github-review, and put back
   anything that only exists here. Diff file by file, not by commit list. While in
   there, close the two non-blocking findings from the review of the 2026-11-07
   change: `scripts/ci_status.py` subscripts the `pressure` dict directly, and the
   dashboard renders `/ undefined` when `load_ceiling` is missing. The dashboard
   half is the consumer's to fix, since the dashboard is not part of this platform.
   **Verify:** the full suite, ruff, ruff format, and mypy are green, and a `/status`
   from the deployed controller has the same keys as one from this tree.
2. **Same for `github-actions-runner`.** Diff the 7 files, decide which side is
   ahead, and make this tree the superset.
3. **Bring the `ci_controller` role to parity with the consumer's inline tasks**,
   task by task. Both carry the work-dir prune units, inline with hardcoded paths
   and here templated; the 2026-08 open question is still the first work item. The
   runner image build moves with it if the role does not cover it yet.
4. **Consumer cut-over, one tree per deploy:** `ci-controller` and the runner image,
   then `github-review` (the consumer's Task 5b). Each step: bump the pin, switch the
   play to the role, run `--check --diff`, have the owner deploy, then delete the
   consumer's copy in the same PR as the switch. The dead `ci-lane-host` copy goes
   in the first PR. A mixed state, deployed from the role but with the copy still
   present, is the failure this plan exists to end, so it must not outlive one PR.
5. **Stop it reopening.** The consumer adds a test that fails when any of the four
   `services/` trees reappears. A drift check across a public and a private repo
   was considered and rejected: it would need this repo's CI to read the private
   one.

## Verification

This plan retires when the consumer has no copy of any of the four trees, every
platform service on its pool is deployed from an `ordago.ci` role pinned by SHA, and
a commit here plus a pin bump there is the only way to change what runs on a host.
At that point invariant 5 is true as written, and `land.config.json`'s justification
for `requireApprovingReview: false` holds again.

## Still open

- Whether the second operator already deploys every tree from the roles. Confirm
  before task 4, so there is one consumer to migrate rather than two.
