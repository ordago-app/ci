---
name: fix-bug
description: RED/GREEN procedure for fixing a reported defect in the CI platform — write the failing test in the right pytest harness first, then fix, then a `<scope>: <imperative>` PR to `main`. Use whenever a defect is reported, a real test failure surfaces, or a pool incident is being investigated. Don't invoke for new behaviour (`tdd`), refactors, or "while I'm here" cleanups.
---

# Fix a bug

Red/green. The regression test is written **before** the fix and committed in the same PR — that's the proof. Most of the non-obvious code in this repo exists because of an incident; the test is how the next agent learns why.

## 1. Reproduce

Pin down: component (dispatcher / scheduler / lane host / runner image / review bot / role), inputs (pool config shape, job labels, host state), expected vs. actual, and **whose pool** it was seen on. If you can't reproduce, don't code: ask the user for the ledger rows / container logs, or read the incident in `docs/incidents/` that sounds like it.

**"Works on my host, fails on theirs" is almost always invariant 4** — a default that names one operator's machine, path or user. Check `test_no_operator_defaults.py` before suspecting logic.

## 2. RED — write the failing test first

Pick the layer. The harness is already set up; use the existing one — don't introduce a new runner.

| Bug surface | Test home | Command |
|---|---|---|
| Admission, placement, ledger, reaping, health | `services/ci-controller/tests/test_<module>.py` | `cd services/ci-controller && uv sync --extra dev && uv run pytest tests/test_<module>.py` |
| Dispatcher ↔ scheduler contract | `test_scheduler_api.py` / `test_scheduler_client.py` / `test_scheduler_parity.py` | same |
| Compose / main wiring | `test_compose_wiring.py` / `test_main_wiring.py` | same |
| Review bot | `services/github-review/tests/` | `cd services/github-review && uv sync --extra dev && uv run pytest` |
| Runner image template or entrypoint | `services/github-actions-runner/tests/` | `uv run --no-project --with pytest --with jinja2 --with pyyaml pytest services/github-actions-runner/tests` |
| Operator script (`validate_pool_config`, ACL generator, …) | `scripts/tests/` | `uv run --no-project --with pytest --with pydantic --with pyyaml pytest scripts/tests` |
| Role interface, default, or guard | `roles/tests/` | `uv run --no-project --with pytest --with pyyaml pytest roles/tests` |

Naming: the test function names the symptom, not the fix — `test_reservation_released_when_host_stops_reporting`, findable from the incident.

**Run the test, see it fail.** A test that passes before the fix is the wrong test. Paste the RED line into the PR body if it isn't obvious from the diff.

## 3. Fix at the right layer

Non-negotiables from `AGENTS.md` that bug fixes commonly violate:

- **Don't hand the scheduler a credential** or a dispatcher another org's placement to "skip the bug". The split is the design.
- **Don't add a default that names a real machine, path or user.** Take it as input.
- **No silent fallbacks.** Don't catch-and-default the failure away — surface it; a lane silently not spawned is how a pool overcommits.
- **Don't put anything private to one operator in the fix** — no tailnet addresses, no private service names.

If the cause is a role and the fix changes what a consumer must set, say so in the PR: consumers pin by SHA and read `README.md`'s interface list on bump.

## 4. GREEN — test passes, repro is gone

- New test passes with the command from step 2; the whole service suite still passes.
- `uvx ruff check . && uvx ruff format --check .` and, for `services/*`, `uv run mypy --config-file=../../pyproject.toml src`.
- If the bug was seen on a live pool, say in the PR how it was (or wasn't) re-verified there — nothing on `main` reaches hardware until the owner bumps their pin, so "merged" is not "fixed on the pool".

## 5. If it was an incident

A bug that took a pool down, leaked lanes, or double-booked a host gets `docs/incidents/<date>-<slug>.md`: what happened, scope, recovery, and a link to the decision or test that now guards it. Write it in the same PR — an incident recorded in a chat message vanishes.

## 6. PR to `main`

- Branch off `main` as `fix/<short-slug>`, in a worktree.
- Commit: `<scope>: <imperative>` — `ci_controller: release a reservation whose host stopped reporting`. **Not** `fix:`. Root cause in the body.
- The PR contains the regression test **and** the fix in the same diff.
- Land with `node scripts/pr-land.js` (see the Autonomy contract in `AGENTS.md`). A hard-stop path hands the PR to a human.

## Required outputs

- [ ] Failing test committed in the right harness per the table.
- [ ] Fix obeys the invariants (no credential crossing, no operator default, no silent fallback, nothing private).
- [ ] Incident note if a pool was affected.
- [ ] PR open against `main` with a `<scope>: <imperative>` commit and root cause in the body.

## Don't

- **Don't ship a fix without a test.** Bugs without regression tests come back — the incidents folder is the record of that.
- **Don't write the test after the fix** — you'll write one that already passes. Red first.
- **Don't fix the symptom layer** when the cause lives one layer up. The contract is the bug.
- **Don't add a try/except to silence the error** — that's a silent fallback.
- **Don't bundle a refactor or "while I'm here" cleanup** into the fix PR. Open a separate one.
