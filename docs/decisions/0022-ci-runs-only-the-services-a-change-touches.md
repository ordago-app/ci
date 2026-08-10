# 22. CI runs only the services a change touches

Date: 2026-08-10

## Status

Accepted.

## Context

`pytest.yml` and `mypy.yml` carried hardcoded service matrices, so every PR ran all seven
service suites plus six typecheck jobs regardless of what it touched. Measured on
2026-08-09, **12 of the last 15 merged PRs touched only `services/ci-controller`** and still
paid the full 13-job fan-out.

Lanes are the binding constraint on this host, not billing: over a 47-day window
`budget_full` deferred 1932 jobs and `lane_ceiling` 1261, and light-lane queue wait was p50
243 s / p90 1310 s. Every wasted lane is queue time taken from a job that needed it.

`locks.yml` already reasoned exactly this way in prose — "deliberately a single job, not a
per-service matrix … `lane_ceiling` is the dominant admission gate on this host" — and that
reasoning had simply never been applied to the two workflows that spend the most.

Two constraints that would normally forbid skipping jobs do not apply here, and both were
verified rather than assumed: branch protection is unavailable on this private repo (the
protection and rulesets endpoints both 403), so a matrix entry that never reports cannot
wedge a PR; and the reviewer treats CI status as prompt context, tolerating a 403 from
`check-runs`, so it does not hard-gate on green.

## Decision

A `changes` job resolves the matrix from the diff and both workflows consume it via
`fromJSON`. `scripts/ci_changed_services.py` owns the rules.

**The service inventory is discovered from the filesystem**, not listed. Any
`services/*/pyproject.toml` (depth 2, for `claude-code/router`) with `src/` and `tests/`
qualifies. The two hardcoded lists this replaced had already drifted apart: `ci-controller`
was in pytest's matrix and missing from mypy's, so the code deciding what runs on this host
was the one service never type-checked.

**It fails open.** An unresolvable diff — a zero SHA from a force-push, a git error, an
unknown path — selects every service. A wrong fan-out costs lanes; a wrong skip merges
untested code.

**Per-service selection is only sound for hermetic suites.** Service tests here are not all
hermetic, so `EXTRA_DEPS` declares the repo files a suite reads from outside its own
directory, and a guard test fails if a service starts reaching out without being declared.
That map is deliberately kept near-empty: the repo-wide assertion suites that dominated it
(deploy wiring, agent overlays) imported none of the service they were filed under and now
live in the top-level `tests/`, running unconditionally.

### Rejected: per-service workflows with native `paths:`

This would save the same lanes with **zero** added latency, since GitHub's path filter needs
no runner, versus the one queue hop (p50 +243 s) the gating job costs. Rejected because
`needs:` cannot cross workflow files, so the green-gated `request-review` trigger — the most
valuable automation in this repo — would have degraded to the poll backstop; and because it
duplicates the shared-path list into seven files that will drift.

## Consequences

Replayed over the last 15 merges, matrix + gating jobs fall from **195 to 106**. The first
PR to demonstrate it in production (#144) selected `["ci-controller"]` alone, running 2
matrix jobs where the static matrix ran 13.

A path-filtered workflow must now match its own file, or a change to it merges unexercised —
`ansible-lint.yml` had exactly that hole. Enforced by
`scripts/tests/test_workflow_path_filters.py`.

A declaration is only as good as its reachability: `docs/runbook.md` is a `ci-controller`
test input, but `pytest.yml` filtered `docs/**` out of its trigger, so the workflow never
dispatched and the dependency was dead. The trigger re-includes the runbook, and a test
fails if any `docs/`-or-`.md` entry in `EXTRA_DEPS` is unreachable through it.

The remaining known weakness, stated in the guard's own docstring: it catches a *new
escaping service*, not a new escape inside one already declared. Widening an existing entry
stays a human judgement.
