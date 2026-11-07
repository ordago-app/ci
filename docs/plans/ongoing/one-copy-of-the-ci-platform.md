# One copy of the CI platform

**Priority:** high

Decided 2026-11-07 by the first operator: there is one copy of the platform, this one.
This repo is the engine: everything the CI platform *does*. A consumer is a place
that runs it, whether a homelab, a VPS or a cloud VM, and holds only its own
configuration: which machines, which repos and classes, which secrets, and which
version. A consumer deploys the `ordago.ci` roles, which run images built here, both
pinned to the same commit. Nothing about one consumer belongs in the engine
(invariant 4), so a new consumer needs no code change.

## Status

| Task | State |
|---|---|
| 1. ci-controller superset | done, #26 |
| 2. github-actions-runner superset | done, #27 |
| 3. Publish images | done, #28. Each GHCR package must be made public once by an org owner after its first publish, or every consumer needs a pull credential |
| 4. Roles run the published images, at parity | `ci_controller` and `ci_lane_host`: #29; `github_review`: #30 |
| 5. Consumer cut-over | not started; needs each machine's owner to deploy |
| 6. Stop it reopening | not started |

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

1. **Make this repo's `ci-controller` the superset.** Done as a three-way merge per
   file. The base is the consumer's tree at the extraction (its commit `00ab32a`),
   one side is this repo, the other is the consumer's tree now. So the extraction's
   own edits survive: no operator-config tests, and the INFO-enabled socket proxy.
   ADRs 0027 and 0032, which the merged code cites, come across with their
   numbers. `review_admission` is now a **required** config key. That is a schema
   change, so every consumer's pool config must carry it before its pin moves past
   this commit. `scripts/ci_status.py` comes across too, with the review finding
   from 2026-11-07 closed: a pressure payload of another shape is named, not a
   crash. (The dashboard's `/ undefined` is the consumer's to fix, since the
   dashboard is not part of this platform.) **Verify:** the full suite, ruff, ruff format and mypy are green,
   and the only differences left against the consumer's copy are the extraction's
   edits.
2. **Same for `github-actions-runner`.** The consumer's copy was ahead: `gh` in
   the image as part of a tested toolbox contract, the runner at 2.338.2 with its
   ARG moved below the Android layers, and the App's Actions: write documented.
   Its toolbox tests lived in the render test this repo dropped, so they come
   across as `tests/test_image_contract.py`.
3. **Publish images.** On every push to `main`, this repo's CI builds the
   dispatcher/scheduler image and the runner image and pushes them to GHCR,
   tagged with the full commit SHA. GitHub-hosted runners, as for the rest of this
   repo's CI (a broken pool must not stop the fix to the pool). Images carry no
   secrets: every credential arrives at deploy or spawn time, as today. This
   touches `.github/workflows/`, a hard stop, so a human merges it.
4. **The roles run the published images and reach parity with the consumer's
   inline tasks.** `ci_controller` takes `ci_image_tag` (the commit SHA, required,
   never `latest`) and pulls instead of syncing source and building. The pool
   config's `runner_image` points at the published runner image. Then go task by
   task through the consumer's inline play; the work-dir prune units are the known
   difference (inline with hardcoded paths, templated here). Done for
   `ci_controller` and `ci_lane_host`: both require `ci_image_tag` (a full SHA),
   pull instead of build, and `ci_controller` refuses a pool config whose
   `runner_image` is another commit's. A consumer must pin a commit at or after
   the images workflow (task 3), because only those commits have images.
   `github_review` still builds its three images on the host; it moves the same
   way before the consumer's Task 5b cut-over.
5. **Consumer cut-over, one tree per deploy:** `ci-controller` with the runner
   image, then `github-review` (the consumer's Task 5b). Each step: bump the pin,
   switch the play to the role, run `--check --diff`, have the owner deploy, then
   delete the consumer's copy in the same PR as the switch. The dead
   `ci-lane-host` copy goes in the first PR. A mixed state, deployed from the role
   but with the copy still present, is the failure this plan exists to end, so it
   must not outlive one PR. The consumer keeps its operator-config tests (the three
   this repo dropped at extraction) by moving them into its own `tests/`.
6. **Stop it reopening.** The consumer adds a test that fails when any of the four
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
