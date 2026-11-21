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

**Seen again on 2026-11-21, in a second form.** Three runs in an operator repo were created
between 11:17 and 12:20 UTC, again while the machine was short of memory. They reported
`status: pending`, not `queued`. Their jobs were `queued` with self-hosted labels and no
runner. A `pending` run can never appear in a `status=queued` listing, so the controller
missed them the same way. Two hours later, `make ci-status` showed 1 of 15 lanes busy and
nothing deferred. Later runs on other refs were served normally. So the sweep must judge
by the job's status, whatever status the run itself reports.

The fix that worked before — cancel, then rerun — worked for the PR run but not for the
`develop` run. Rerun that run once more (attempt 2) and it stayed `pending`
with **zero jobs** for at least 4 minutes, and it was missing from the branch's run listing.
With no jobs, the controller has nothing it could have served. So the cause is on GitHub's
side, before any job is created. Treat this as a third case: the sweep can only report it,
not fix it.

## Proposal

A slow sweep, about every 10 minutes, that does not trust the listing:

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
