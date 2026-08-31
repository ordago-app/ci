---
name: tdd
description: Feature-driven TDD with vertical RED→GREEN→REFACTOR per behaviour, in this repo's pytest harnesses. Use when building new dispatcher/scheduler/reviewer behaviour, a new role interface, or the user says "TDD this", "red-green-refactor", "test-first". For defects use `fix-bug` instead — same loop, different entry point.
---

# TDD — feature driven

`fix-bug` is for defects (the symptom is the test). `tdd` is for new behaviour (the spec is the test). Same RED→GREEN loop; different starting point.

**Tests are the specification here.** `services/ci-controller` carries ~5.5k LOC of them, and they encode incident lessons the code does not restate. A behaviour without a test is not a behaviour this repo has.

## Philosophy

**Tests verify behaviour through public interfaces, not implementation details.** Code can change entirely; tests shouldn't. A test that fails when you rename a private helper but the observable behaviour didn't change is a bad test.

**Good test:** "a reservation whose lane host stops reporting is released after the grace period" — reads like a spec, survives refactors.

**Bad test:** asserts an internal helper was called with arg X — implementation detail, breaks on refactor, proves nothing about behaviour.

## Anti-pattern — horizontal slicing

**Do NOT write all tests first, then all implementation.** That produces tests against *imagined* behaviour, tests of shape instead of behaviour, and tests insensitive to real changes.

```
WRONG (horizontal):
  RED:   test1, test2, test3, test4
  GREEN: impl1, impl2, impl3, impl4

RIGHT (vertical):
  RED→GREEN: test1 → impl1
  RED→GREEN: test2 → impl2
  ...
```

## Workflow

### 0. Pick the harness

Same table as `fix-bug` step 2. All of these run in seconds with nothing else up.

| Surface | Test home | Command |
|---|---|---|
| Dispatcher / scheduler / ledger / admission | `services/ci-controller/tests/test_<module>.py` | `cd services/ci-controller && uv sync --extra dev && uv run pytest tests/test_<module>.py` |
| Review bot | `services/github-review/tests/` | `cd services/github-review && uv sync --extra dev && uv run pytest` |
| Runner image (compose template, entrypoint) | `services/github-actions-runner/tests/` | `uv run --no-project --with pytest --with jinja2 --with pyyaml pytest services/github-actions-runner/tests` |
| Operator scripts | `scripts/tests/` | `uv run --no-project --with pytest --with pydantic --with pyyaml pytest scripts/tests` |
| Ansible roles (interface, defaults, guards) | `roles/tests/` | `uv run --no-project --with pytest --with pyyaml pytest roles/tests` |

The commands mirror `.github/workflows/pytest.yml` exactly — if they drift, fix the workflow or this table in the same commit.

### 1. Plan (before any code)

Confirm with the user (or take your own pick under `ship-a-feature`):
- What's the public interface? (config keys, API routes, role variables, CLI flags)
- Which behaviours are tested? Prioritise the ones an incident would be written about.
- Does this cross the credential line or pick an operator default? (AGENTS.md invariants.) If so, the design is wrong — stop before writing a test for it.

You can't test everything. Focus on critical paths and the state machines (reservation lifecycle, health, reaping), not every permutation.

### 2. Tracer bullet — first RED→GREEN

One test that proves the path works end-to-end through every layer it touches:

- **RED:** write the test. Run it. See it fail with a meaningful error (not `ImportError` — that's just a stub missing).
- **GREEN:** write the minimum code to pass. Resist the urge to add more.

### 3. Incremental loop

For each remaining behaviour: **RED** next test → fails; **GREEN** minimal code → passes.

- One test at a time
- Only enough code to pass the current test
- Don't anticipate future tests (YAGNI)
- Keep tests focused on observable behaviour

### 4. Refactor (only while GREEN)

Extract duplication, deepen modules (small interface, complex implementation hidden), reconsider names — agents read identifiers, not comments. Run tests after each refactor step. **Never refactor while RED.**

## Mocking — when, and how little

- **In-process (ledger, admission, placement):** no mocks. Drive the real objects with an in-memory store.
- **Docker and GitHub:** go through the existing adapters (`docker_adapter.py`, `github_adapter.py`) and the fakes the test suites already carry. Don't roll a new mock of the Docker SDK.
- **The other process (dispatcher ↔ scheduler):** test each side against the HTTP contract with the existing test client; `test_scheduler_parity.py` is the pattern for keeping the two in step.
- **Third-party APIs:** mock at the boundary, never deeper.

## Per-cycle checklist

- [ ] Test describes behaviour, not implementation
- [ ] Test uses the public interface only
- [ ] Test failed in RED with a meaningful error
- [ ] Code is minimal — no speculative features
- [ ] No operator default introduced (`test_no_operator_defaults.py` still passes)
- [ ] No silent fallback introduced

## Don't

- Don't write all tests first.
- Don't refactor while RED.
- Don't keep a passing test you wrote without seeing it fail — you can't trust it.
- Don't bundle a refactor that isn't covered by the tests in the current scope. Open a separate PR.
- Don't add a `pyproject.toml` to a test-only tree (`roles/tests`, `scripts/tests`, the runner image) to make it "a proper package" — the ad-hoc `uv run --no-project` jobs are deliberate.
