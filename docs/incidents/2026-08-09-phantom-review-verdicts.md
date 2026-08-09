# Phantom review verdicts

## Symptom

On PR #130, a `REQUEST_CHANGES` verdict was read as a real objection and blocked a merge
decision. Separately, the same day, an agent-opened PR's mandated automated review appeared to
have run (a normal successful poll was logged) but never actually reviewed the diff.

## Root cause

Two independent issues collided:

- `personal/agent-review.yml` sets `review_mode: labeled`, so `github-review` silently skips
  any PR without the `ai-review` label — logging a normal poll either way. Adding the label
  after `opened` is too late: `pytest.yml`'s CI-green review trigger reads
  `github.event.pull_request.labels.*.name` off the payload snapshotted at `opened`.
- `services/github-review/src/worker.py` returns a synthetic `REQUEST_CHANGES` with
  `escalated=True`, without reading the diff, once `rounds_for(repo, pr) >= MAX_REVIEW_ROUNDS`
  (default 5, counted as distinct posted head SHAs — so docs-only pushes burn rounds too). The
  verdict string is identical to a genuine objection, and it's absorbing: once capped, no later
  push can produce a clean review.

## Fix

None needed for the label issue beyond process (see below). PR #135 makes the round-cap surface
as `BUDGET_EXHAUSTED` in the workflow instead of a plain `REQUEST_CHANGES`.

## What still bites

- Create agent PRs with the label already attached (`gh pr create --label ai-review …`), never
  add it after the fact with `gh pr edit`.
- Before treating a `REQUEST_CHANGES` as a real finding, check `escalated` in the `/reviews`
  response or the container logs. Tell a real review from a capped one by the request pattern:
  real review does `GET /pulls/N/files` → `GET /commits/<sha>/check-runs` →
  `POST /pulls/N/reviews`; a capped one does only `GET /pulls/N`.
- Batch review fixes into one push — five rounds go fast when plan/doc updates each burn one.
- `homelab` has `run_ci_first: true`, so the review lands after CI is green, not in parallel;
  `ordago-apps` has `run_ci_first: false`.
