# Every commit on main has its images, and a role says so before it changes anything

**Priority:** medium — 7 of the 29 commits on `main` since images were first published cannot be deployed in full; the only guard is a sentence in the README
**Gate:** decision:Should every push to `main` publish its images (no queued run dropped), and should each role check that its images exist before it changes anything on the host?

**Goal:** A commit an operator pins is a commit whose images exist, and when one does not, the deploy stops at its first task with a message that names the missing image, instead of failing halfway on the operator's machine.

## Context

As of `30c5e5c` (2026-11-22). Window: the 29 first-parent commits on `main` from `8b5f5e2`
(2026-11-07, images first published) to `30c5e5c`. Each was matched to its `images` run
(`gh run list --workflow images.yml --branch main --event push`) and each of its six tags
was looked up on GHCR (`ghcr.io/ordago-app/<image>:<sha>`, plus `ci-runner:<sha>-light`).

**7 of 29 commits lack a complete set. Two different causes.**

| Commit | Run | Missing on GHCR | Cause |
|---|---|---|---|
| `1984a58` roles: run the images published at the pinned commit (#29) | cancelled | all six | replaced while pending |
| `3fe4818` github_review: run the images published at the pinned commit (#30) | cancelled | all six | replaced while pending |
| `5f08299` plans: task 4 of one-copy is done | cancelled | all six | replaced while pending |
| `8473226` plans: the dispatcher survives a reboot | cancelled | all six | replaced while pending |
| `8b5f5e2` ci: publish every platform image to GHCR (#28) | failure | `github-review`, `github-review-claude` | `Publish`: `error from registry: denied` (first publish) |
| `e9e7c76` plans: homelab's controller runs from the role | failure | `ci-runner`, `ci-runner-light` | `Build` failed, never rerun |
| `a6ba36e` github_review: optional state, clones and codex login dirs (#35) | failure, 2 attempts | `ci-runner`, `ci-runner-light` | `Build`: `curl: (56) Connection died` downloading pnpm |

- **Dropped runs (4).** `.github/workflows/images.yml` sets
  `concurrency.group: images-${{ github.ref }}` with `cancel-in-progress` true only for
  pull requests. GitHub's documentation for `concurrency` (read 2026-11-22,
  `github/docs` `data/reusables/actions/actions-group-concurrency.md`): "By default, any
  existing `pending` job or workflow in the same concurrency group will be canceled and
  the new queued job or workflow will take its place." So on `main` one run executes, one
  waits, and a third push cancels the waiting one. The run timestamps match: on
  2026-11-07 `8b5f5e2` ran 14:04–14:11 UTC and the next three pushes (14:06:54, 14:07:31,
  14:07:45) were each cancelled by the one after; on 2026-11-08 `8473226` (00:36) was
  cancelled by `5836ef1` (00:37) while `15677b4` was still running. A successful run
  takes about 8 to 15 minutes once it starts, so three pushes inside that span drop one.
  Two pushes do not: in the five such pairs in the window no run was cancelled.
- **Failed runs (3).** A partial set: some images exist at the SHA and others do not.
  (A fourth, `a0c4be6`, failed its first attempt and published on a rerun 16 minutes later,
  so it is complete today.)
  No workflow change can prevent these; an upstream download or the registry failed.
- **Docs-only pushes publish too, by design.** No workflow has a `paths:` filter
  (`.agents/land.config.json` `ciPaths: ["**"]`), and the header of `images.yml` says
  "Every merge to main publishes each image tagged with that commit's full SHA". 16 of the
  29 commits touch only plans or docs. The roles and the images are pinned by one SHA
  (`ci_image_tag`, README "the code that deploys and the code that runs are one commit"),
  so any commit on `main` has to be pinnable.
- **The existing guard is documentation.** `aedc44d` readme: pin a commit whose images
  were published — three lines in `README.md`, added the day the gap was first met.
- **What the operator sees today.** All three roles assert only the tag's shape
  (`ci_image_tag is match('^[0-9a-f]{40}$')`). The first contact with the registry comes
  late:
  - `roles/ci_controller/tasks/main.yml`: "ci-controller image pinned for compose" rewrites
    `.env` with the new tag, and only then "The local lane runner image is present" and
    "ci-controller stack up" pull. A missing image fails the play after the host's files
    already name it.
  - `roles/github_review/tasks/main.yml`: "Compose variables for this instance" writes the
    three image references about 130 lines before "The review sandbox images are present".
  - `roles/ci_lane_host/tasks/main.yml`: "The lane runner image is present" runs before the
    compose file is written, so this role already fails early, with Docker's own
    "manifest unknown" as the message.

## Design

Two changes, each useful alone; they ship as two PRs.

**1. No push to `main` loses its run.** In `.github/workflows/images.yml`, a push gets a
concurrency group of its own, and pull requests keep today's behaviour (illustrative):

```yaml
concurrency:
  group: images-${{ github.event_name == 'pull_request' && github.ref || github.sha }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

Runs on `main` then execute side by side. Nothing needs them in order: every tag is the
commit's own SHA and there is no `latest`, so two runs never write the same tag. The
header comment's "Every merge to main publishes…" becomes true, and the README sentence
shrinks to the remaining case (a run that failed).

Cost: the repository is public, so GitHub-hosted standard runners are not billed. The
extra work is the runs dropped today: 4 in 15 days, each about 10 minutes across six jobs,
one of them the 16 GB runner image.

**2. A role checks its images first.** Right after the `ci_image_tag` shape assert, each
role looks up every image it is about to run, without downloading it (illustrative:
`docker manifest inspect <ref>` on the target host, which uses the same registry login
the pull would), and fails before any file is written:

```
No image at ghcr.io/ordago-app/ci-runner:<sha>. The images run for that commit did not
publish it. Pin a commit whose images run on main succeeded:
  gh run list --repo ordago-app/ci --workflow images.yml --commit <sha>
```

- `ci_controller`: `ci-controller:<tag>` and every `runner_image` the pool config names
  (already collected by "Every runner_image in the pool config is this commit's").
- `ci_lane_host`: `ci_lane_runner_image`.
- `github_review`: `github-review`, `github-review-claude`, `github-review-codex`.

No role variable is added or changed, so a consumer's pin bump has nothing to react to.

**Alternatives considered:**
- `concurrency.queue: max` (keeps up to 100 pending runs, in order). Documented for
  github.com in the same reference page, and it also drops nothing. It lost because the
  order buys nothing here and a burst of merges would publish serially, the last one
  waiting behind every 10-minute run before it; it is the fallback if side-by-side runs
  ever prove a problem.
- Keep the drop and only make it visible (change 2 alone). Cheapest, and it never touches
  a hard-stop path, but a quarter of `main` stays unpinnable and the workflow header stays
  false.
- Pull every image at the top of the role instead of a lookup. No new mechanism, but a
  partial set (the `e9e7c76` and `a6ba36e` case) would download gigabytes before failing.
- Skip docs-only pushes with a `paths:` filter. Rejected: it makes more commits
  unpinnable, the opposite of the goal, and breaks the "no path filters" rule the merge
  bar rests on.

**Testing strategy:** the role tests in `roles/tests/` read the task files as YAML and
assert on order and content. Add one test per role: the image lookup comes directly after
the pin assert and before the first task that writes a file, and it covers every image the
role later pulls or names (derive the list from the role's own `docker_image` tasks and
compose variables, so a new image cannot be added without its check). For the workflow,
a test alongside them that loads `images.yml` and asserts a push's concurrency group
contains `github.sha`; the real proof is three pushes close together on `main` after it
lands, all three publishing.

## Paths

As of 2026-11-22:

- `.github/workflows/images.yml`
- `README.md`
- `roles/ci_controller/tasks/main.yml`
- `roles/ci_lane_host/tasks/main.yml`
- `roles/github_review/tasks/main.yml`
- `roles/tests/test_ci_controller_role.py`
- `roles/tests/test_ci_lane_host_role.py`
- `roles/tests/test_github_review_role.py`
- `roles/tests/test_images_workflow.py` (new)

## Approval

**Needs your yes** — the `Gate` question; recommended answer: yes to both, because the
drop is measured (4 commits in 15 days, two of them code merges), fixing it costs nothing
on a public repository, and the role check turns the failures no workflow can prevent
into a clear message before the host is touched.

`.github/workflows/**` is a hard-stop path (`.agents/land.config.json`): the PR for
change 1 never self-merges and is handed over, however green. Change 2 touches only
roles and their tests and lands on green CI.

## Out of scope

- Publishing the seven commits that lack images today. A rerun of those runs would do it
  (`gh run rerun <id>`, `--failed` for the three partial ones); nobody is known to need
  them.
- Alerting when an `images` run on `main` fails. `e9e7c76` failed and was never rerun,
  and nothing reported it; that is a separate gap.
- Retrying flaky downloads inside the runner image's `Dockerfile` (a hard-stop path of
  its own).
- Whether a docs-only push should rebuild a 16 GB image at all, and how much GHCR storage
  one image set per commit takes.
- An operator-run report of what a pin bump changes between two commits (role variables
  added, removed or changed; which images exist, per role). The role check here covers
  the image half at deploy time; the variable comparison was drafted in the same pass
  and not filed.
