# Review status contract

**Priority:** low — no drift incident yet, but a rename of `/status` from github-review would pass both suites and quietly blank the dashboard's stuck-review alarm
**Gate:** decision:Should github-review's `/status` payload be pinned by a committed sample that a repo-level test checks against the dashboard's expectations, with no cross-service imports?

**Goal:** A rename or removal of a field in github-review's `GET /status` fails CI on the PR that makes it. Today the dashboard falls back to a default and shows a zero or an empty card in prod, and nobody is told.

## Since the move (2026-11-08)

This idea was written in homelab, when github-review and its consumer, homelab's dashboard,
lived in one repo. They no longer do: the producer is here, the dashboard stays in the
consumer's repo. What changes:

- **Step 1 (the producer sample) is this repo's.** `status.sample.json` and the TypedDicts
  live here, kept honest by a real-worker test.
- **Steps 2 and 3 are the consumer's.** Its fixture and its join test read the sample from
  the `ordago.ci` commit it pins, not from a sibling tree. A pin bump that renames a field
  then fails in the consumer's PR that makes the bump, which is the moment it matters.

The rest is the original text, with the consumer's paths as they were.

## Context

As of `2f3cc77` (2026-10-17), 90-day window:

- `services/dashboard/src/normalize.py` (churn 6, 262 lines), `services/dashboard/src/static/index.html` (churn 7, 964 lines) and `services/github-review/src/worker.py` (churn 10, 431 lines) changed together three times (3c5c42a #150, 62b4464 #161, 29a7a5d #213). In each case the PR built one feature across both services: 62b4464 *dashboard: alarm on stuck reviews…* (#161) added `stuck` to `api.py::create_app.status` and `normalize.py::normalize_reviews` together, and 29a7a5d *dashboard: show reviewer subscription quota…* (#213) did the same with `reviewer_quota`. That co-change is inherent: a new field always touches both the producer and the consumer. It is not what this plan removes.
- What this plan does remove is **silent drift**. Both sides test against their own hand-written picture of the payload, and nothing checks that the two pictures match:
  - `services/github-review/tests/test_api.py::status_client` builds `/status` from a `MagicMock` worker. `reviewer_quota` and `degraded_providers` come back as whatever the mock returns, so the full payload is never produced by real code in any test.
  - `services/dashboard/tests/test_normalize.py` feeds hand-written dicts (`_status(...)`, `CODEX_OUTAGE`) into `normalize_reviews` and `normalize_reviewer`.
  - By design, the dashboard is a tolerant reader: homelab ADR 0024 isolates each source so a dead subsystem costs only its own card, and the `normalize.py` module docstring makes it field-level ("Every field is defaulted. A subsystem that predates a field should cost the page one empty section, never the whole card"). `normalize_reviews` reads `raw.get("stuck", 0)`, and the attention badge in `index.html` sums `down + stuck`.
  - So renaming `stuck`, say to `stuck_count`, updates `test_api.py`, leaves `test_normalize.py` green, and leaves the badge permanently dark in prod. That badge is the one signal that a review is out of retries.
  - Renaming `reviewer_quota` would render the quota card as an empty list instead. homelab ADR 0024 shows this risk class is real: the first dashboard deploy reported two healthy services as down because it assumed a body shape that no service had promised (3001eea).
- Two semantic bugs have shipped on this endpoint: 83d62f2 (*bound `stuck` to recent failures*) and the `reports_quota` inference described in `worker.py::ReviewWorker.reviewer_quota`. A shape contract would not have caught either one. This plan is about shape only.
- Dependency category: **remote but owned**. These are two of our own services across an HTTP boundary, in separate `uv` projects, so they cannot import each other. Each one must stay hermetic: `scripts/ci_changed_services.py` selects per-service suites, and its `EXTRA_DEPS` map is meant to stay "as close to empty as possible".

## Design

A consumer-driven contract made of two committed JSON files and one repo-level test. No code crosses the service boundary.

1. **Producer sample, kept honest by github-review.**
   - New file `services/github-review/tests/fixtures/status.sample.json`, holding one `/status` body.
   - A new test in `test_api.py` builds it from **real** code: a real `ReviewJobStore`, plus a real `ReviewWorker` whose providers are the existing test stubs, with one Claude-shaped provider that has a `last_quota` and one codex-shaped provider with an outage.
   - The test asserts that the body has exactly the sample's keys and value types at every level, including `active[]`, `reviewer_quota[].quota` and `.outage`. Values are not compared.
   - For the payload to have a shape a test can name, `api.py` gains `TypedDict`s for it (illustrative: `StatusPayload`, `ActiveJob`, `QuotaRow`), returned from `status()`. They are types only, with no runtime validation and no `response_model`, so the served JSON does not change.
2. **Consumer fixture, kept honest by the dashboard.**
   - New file `services/dashboard/tests/fixtures/github_review_status.json`.
   - `test_normalize.py` runs `normalize_reviews` and `normalize_reviewer` on it. For every field the page renders, the test asserts it was read from the payload and did not fall back to its default.
   - The fixture holds only the keys the dashboard reads, so a field that github-review adds and the dashboard ignores never breaks anything.
3. **The join.**
   - New file `tests/test_review_status_contract.py`, in the dependency-free repo-level harness. Like `tests/test_deploy_wiring.py`, it reads both trees and imports neither.
   - It asserts that every key path in the consumer fixture exists in the producer sample with the same JSON type. Nullable fields are declared nullable on both sides.
   - `pytest.yml`'s `repo-tests` job already runs `tests/` unconditionally on every PR that starts the workflow (anything outside `docs/**` and `*.md`), so a github-review-only code change is checked with no new `EXTRA_DEPS` entry.

How the existing suites change:
- `test_status_carries_the_reviewer_quota_rows` and `test_status_serves_the_quota_rows_verbatim` keep their mocks. They test pass-through, and the new real-worker test owns the shape.
- In `test_normalize.py`, `_status()` builds from the fixture instead of an inline dict, so the hand-written baseline is the contract itself. The cases for individual rows stay.
- Correct the `normalize.py` docstring line "tested directly against recorded payloads" so it names the fixture.

homelab ADR 0024 holds: the dashboard stays read-only, stateless and tolerant (defaults stay; an older github-review still renders). The contract tests only check that the current producer still provides what the current consumer reads.

The pattern applies as-is to ci-controller's `/status` (`normalize_ci`) and to claude-router's session list (`normalize_sessions`). Those are out of scope here, and this plan goes first.

**Alternatives considered:**
- **A: a minimal typed interface.** Add only `TypedDict`s or a pydantic `response_model` on `/status`. This types the producer but cannot stop the consumer from reading a key that no longer exists. It is kept as part of step 1, not used on its own.
- **B: a shared vendored model.** Copy the same pydantic model into both services and add a byte-equality sync test. This is a second copy to maintain, against the North Star's "consolidate duplicates". Strict parsing on the dashboard side would also break the tolerant-reader rule (`normalize.py`'s "never the whole card", on top of homelab ADR 0024's per-source isolation), because an older github-review would cost the whole card.
- **A shared package that both services import.** This is ruled out by the per-service `uv` projects and the hermetic-suite rule, and it is far heavier than the risk calls for.
- **A recorded production payload.** It cannot be regenerated in CI and goes stale as soon as a field is added.

**Testing strategy:**
- The producer's shape test runs against a real worker and store. This replaces the mocked worker as the source of truth for the shape.
- The consumer's normalizer tests run against the fixture.
- One repo-level test joins the two.
- No shallow tests are added. `_status()`'s inline baseline is replaced, not layered on.

## Paths

As of 2026-10-17:

- `services/github-review/src/api.py` (TypedDicts for the `/status` body)
- `services/github-review/tests/test_api.py`
- `services/github-review/tests/fixtures/status.sample.json` (new)
- `services/dashboard/src/normalize.py` (docstring only)
- `services/dashboard/tests/test_normalize.py`
- `services/dashboard/tests/fixtures/github_review_status.json` (new)
- `tests/test_review_status_contract.py` (new)

## Approval

**Needs your yes.** The gate question is whether to pin github-review's `/status` with a committed sample that a repo-level test checks against the dashboard's expectations, with no cross-service imports. Recommended answer: yes. It turns a silent dark badge into a red check, using only the test patterns the repo already has: the repo-level `tests/` harness and per-service fixtures. It adds no runtime code and no `EXTRA_DEPS` entry.

`POST /reviews` now enqueues and returns at once, and `GET /reviews/{owner}/{name}/{pr}/{head_sha}` reads one head's state (workspace plan `review-ci-scheduling`). Neither changes the `/status` shape.

## Out of scope

- Contracts for ci-controller `/status` and claude-router sessions: the same pattern, to be filed separately once this one has proved itself.
- Any change to `/status` semantics (`stuck` window, quota fields) or to the page.
- Making the dashboard strict. Its defaults stay, per homelab ADR 0024.
- `POST /reviews` and `GET /reviews/…`. Their consumer is the `review` helper, not the dashboard.
