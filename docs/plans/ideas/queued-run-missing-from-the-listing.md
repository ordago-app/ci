# A queued run GitHub leaves out of its listing never gets a lane

**Priority:** medium

## What was observed

2026-11-20. An operator repo's `pytest` run (created 02:08 UTC) sat `queued`
for almost five hours with no runner. It was created while powerserver was thrashing (see
[`lane-memory-cap.md`](lane-memory-cap.md)), but it kept waiting long after the host
recovered: at 06:58 UTC, the second lane host was offering 4007 MB with no lanes running.

The controller finds work by polling `GET /repos/{repo}/actions/runs?status=queued`. GitHub
contradicted itself about that run:

- `GET …/actions/runs/{id}` returned `status: queued`, and both of its jobs were
  `queued` with no runner.
- `…/actions/runs?status=queued` returned an empty list.
- `…/actions/runs?head_sha=<head>` listed the head's four other runs, but not this one.

The run was missing from GitHub's listings, so the controller never learned it existed.
`make ci-status` showed nothing waiting for that repo, and the run would have stayed queued
until GitHub's 24-hour expiry.

**Workaround that worked:** `gh run cancel <id>`, then `gh run rerun <id>`.
After that, attempt 2 appeared in the `status=queued` listing within 20 seconds.

**Seen again on 2026-11-21 and 2026-11-22, in a second form — now fixed.** Runs reported
`status: pending`, not `queued`, while their jobs were `queued` with self-hosted labels and no
runner. A `pending` run never appears in a `status=queued` listing, so the controller missed
them: an Órdago `develop` "Plans map" run sat like that from 2026-11-21 13:54 UTC for a day.
`list_queued_jobs` now reads both the `queued` and the `pending` listings and lets the job's
own status decide, and `cancel_queued_run` treats a `pending` run like a `queued` one.

What was recorded here as a third case, a rerun that "stayed `pending` with **zero jobs**",
was not GitHub losing the run. That workflow uses a per-ref `concurrency` group with
`cancel-in-progress: false`, and GitHub holds a newer run of the group at `pending` with no
jobs while an older one is unfinished. The older one was the stuck `pending` run above, so the
newer one waited behind it. Serving the stuck run releases the group; nothing more is needed.

## Proposal

What is left is the first form: a run that is `queued` but missing from every listing. A slow
sweep, about every 10 minutes, that does not trust the listing:

1. For each onboarded repo, list open PRs' head SHAs and their check-runs. Check-runs come
   from a different index, and this run's were correct throughout ("config + script suites:
   queued").
2. Any check-run that has been `queued` for more than 15 minutes, has a self-hosted label
   this pool serves, and whose run is missing from the `status=queued` listing is a ghost.
3. Record a `ghost_queued_run` event and show it in `make ci-status`.
   Open decision: should the controller also cancel and rerun the run itself (the workaround
   above), or only report it? Pick: report only at first, and auto-rerun once a few
   sightings confirm the rerun is always safe. A rerun restarts the whole run and needs the
   GitHub App's `actions: write`.

## Not in scope

Why GitHub's listing drops a run. That is GitHub's side; the sweep only has to notice it.
