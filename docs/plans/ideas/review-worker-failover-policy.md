# The review worker's provider policy becomes its own module

**Priority:** medium — `worker.py` is the hottest Python file in the repo (churn 9 in 90 days, 727 lines, no fix commits), and three features in three weeks each widened the same class
**Gate:** decision:Should github-review's provider policy (failover walk, outage tracking, quota rows, disclosure banner) move out of `ReviewWorker` into its own module with direct tests, as a pure refactor that leaves `/status` and every posted text unchanged?

**Goal:** `ReviewWorker` keeps the job lifecycle only, and the provider policy lives in one object that is tested without a queue, a job store, a GitHub client or a worktree, so the next change to failover or quota reporting touches one small module and its own test file.

## Context

As of `30c5e5c` (measured 2026-11-21, re-checked against `origin/main` 2026-11-22), 90-day window (83 commits):

- `services/github-review/src/worker.py`: churn 9, 727 lines, score 86, no test named after it. It changes together with `providers/claude.py` (5 times), `job_store.py`, `provider.py` and `git_worktree.py` (4 each), `api.py`, `config.py` and `main.py` (3 each).
- `ReviewWorker` holds two things. The **job lifecycle**: `start`, `tick`, `_runner`, `run_next`, `_process`, `_reap_stuck`, `_recover_orphans`, `_carry_approval`, `_budget_spent`, `_skip_for_budget`, `_after_failure`, `_give_up`, `request_review`. And the **provider policy**, about 200 lines: `ProviderOutage`, `degraded_providers`, `reviewer_quota`, `review_chain`, `quota_ranking`, `is_degraded`, `_review_with_failover`, `_record_run`, `_disclose`, `_report_outage`, plus the state `_outages`, `_last_ok` and the `_health` lock that guards them.
- Three commits in three weeks added policy to the class: 37f04d4 *github-review: fall back to Claude when the primary provider cannot review (#20)*, 4357f26 *github-review: record what every paid run cost, per account (#23)*, 4878b63 *github-review: rank Claude subscriptions by the weekly credit they would lose (#37)*.
- The policy touches the lifecycle at exactly three points: `_process` calls `review_chain`, `_review_with_failover` and `_disclose`; `_review_with_failover` writes one store row per paid run through `_record_run`; `_recover_orphans` calls `discard` on every provider. Everything else in it is read by `/status` (`api.py::create_app.status` reads `worker.degraded_providers()`, `worker.reviewer_quota()`, `worker.quota_ranking()`).
- What the mix costs today, all checked in the tree:
  - **Policy is tested only by driving the whole worker.** All 12 failover tests in `tests/test_provider_failover.py` that run a review (chain advance, ordinary error stops the chain, whole chain down, outage cleared on recovery, session cleanup, run counting, per-account cost) each build a config file, a SQLite store, `FakeGitHub` and `FakeWorktrees`, and go through `tick_and_drain`: poll, enqueue, claim, prompt build, then the provider walk. `test_degraded_providers_are_reported_and_cleared` has to enqueue a second PR by hand so a job reaches a provider again.
  - **The quota-row tests reach into private state.** `_quota_worker` builds a `ReviewWorker` from four `MagicMock`s (config, store, github, worktrees) only to call `reviewer_quota()`, and two tests write `worker._outages[...]` and `worker._last_ok[...]` directly because there is no cheaper way to put a provider into an outage.
  - **`tests/test_quota_rank.py::test_codex_keeps_first_place_whatever_the_subscriptions_read`** builds a config, a store and a full worker with `providers={}` to call one method, `review_chain`.
  - **Dead code hides in the class.** `ReviewWorker.is_degraded` has no caller in `src/` or `tests/` (`git grep is_degraded origin/main` returns only its definition).
- Dependency category: **in-process**. The walk does no I/O of its own: providers are already behind the `ReviewProvider` protocol (`provider.py`), ranking is already pure (`quota_rank.py::order_chain`, `rank`), and the only store call is the per-run cost row.

## Design

One new module, `services/github-review/src/provider_chain.py`, holding one class and the outage record. `ReviewWorker` builds it in its constructor from the arguments it already takes (`config`, `providers`, `quota_ledger`) and exposes it as a read-only property, so `main.py` and the 23 test call sites that construct a worker do not change.

By responsibility (signatures are illustrative):

```python
class ProviderChain:
    def __init__(self, *, config, providers, quota_ledger, clock=time.time): ...

    # used by the worker
    def order(self, configured: list[str]) -> list[str]            # was review_chain
    def review(self, job, worktree, timeout_seconds, prompt, configured,
               *, on_run: Callable[[str, float | None], None]) -> ChainReview
                                                                   # was _review_with_failover
    def discard(self, job) -> None                                 # every provider, for orphan recovery

    # read by /status
    def degraded(self) -> list[dict]                               # was degraded_providers
    def quota_rows(self) -> list[dict]                             # was reviewer_quota
    def ranking(self) -> dict | None                               # was quota_ranking

@dataclass(frozen=True)
class ChainReview:
    result: ReviewResult
    used: str
    preferred: str
    outages: list[ProviderUnavailable]
    def disclosed_body(self) -> str                                # was _disclose
```

- `ProviderChain` owns `_outages`, `_last_ok` and their lock, the provider dict, the ledger, and the stderr outage line (`_report_outage`). The lock then guards exactly one object's state.
- `on_run(account, cost_usd)` is the one seam back to the lifecycle: the worker passes a closure over `store.record_provider_run` and its log line (`_record_run` stays in the worker, because it is a store write about a job). The chain never imports `job_store`.
- `_process` shrinks to: `review = self._chain.review(...)`, post `review.disclosed_body()`, `mark_posted(provider=review.used, review_body=review.result.body)`. Error behaviour is unchanged: only `ProviderUnavailable` advances, `ReviewLimitExceeded` and every other error propagate to `_process`'s handlers, and a fully down chain raises the same `RuntimeError("no review provider could run: …")`.
- `api.py::create_app.status` reads the three fields from `worker.chain`; the served keys (`degraded_providers`, `reviewer_quota`, `quota_ranking`) and their values are byte-identical. The worker's three pass-through methods are deleted, not kept as aliases, so the callers that read them move to `worker.chain`: `tests/test_concurrency.py` (one assertion on `degraded_providers()`) and the mock worker in `tests/test_api.py`.
- The comment in `providers/claude.py::ClaudeReviewProvider.__init__` that points at "`_outages` in the worker" is updated to name the chain.
- `is_degraded` is deleted.
- `clock` is injected so a test can set "down since" and `last_ok` without sleeping or writing private fields.

Order against other plans: none has to build first. `review-status-contract` (ideas) pins the `/status` shape with a real-worker test; whichever of the two lands second rebases on the other, and if the contract lands first it becomes this refactor's guard. `conversational-review-loop` (ideas) changes `_budget_spent` and the job identity, both of which stay in the worker. The workspace plan `review-ci-scheduling` Phase 4 tunes concurrency and tier rules and does not touch the provider walk.

**Alternatives considered:**
- **A, minimal: move only the health state** (`_outages`, `_last_ok`, the two `/status` readers) into a `ProviderHealth` object and leave the walk in the worker. Fixes the private-field pokes, but the walk, the part with the rules ("only `ProviderUnavailable` advances", "count a run only once the model read the diff"), is still reachable only through a full worker.
- **B, common caller trivial: a module-level `review_with_failover(...)` function plus a separate health object.** The walk both reads and writes health on every step, so the two would be passed around as a pair everywhere; one object is the honest shape.
- **C, most extensible: a pluggable policy interface** (strategy objects for ordering and for failover). There is one ordering rule and one failover rule; an interface with a single implementation is a shallow module.
- **Inject the chain into `ReviewWorker` from `main.py`.** Cleaner on paper, but it rewrites 23 test constructions to build two objects for no added test reach; the worker's constructor is already where this composition lives.

**Testing strategy:** tests at the new boundary, replacing the through-worker ones rather than layering on them.
- New `tests/test_provider_chain.py` builds a `ProviderChain` from `ScriptedProvider`s, a config and a list that records `on_run` calls. It takes over, from `test_provider_failover.py`: chain advance, an ordinary error does not advance, both accounts tried, whole chain down names every provider, outage reported then cleared, every session cleaned up, the second account going down, the `read_diff` run-count rule, per-account cost, and all seven quota-row tests (now with no `MagicMock` collaborators and no writes to private fields). From `test_quota_rank.py`: the two worker-level ranking tests.
- The banner text is asserted directly on `ChainReview.disclosed_body()`.
- Kept through the worker, because they are about the join: one failover end to end (the posted body carries the banner, the job row names the provider that answered, `spend_by_account` charges each account), the cap case (`ReviewLimitExceeded` gives up instead of failing over), and whole-chain-down ends in a failed job whose `last_error` names every provider.
- `test_api.py`'s `/status` tests keep asserting the same keys; only where the mock worker supplies them changes.
- `classify_unavailable` tests stay where they are; they test `provider.py`.

## Paths

As of 2026-11-22:

- `services/github-review/src/provider_chain.py` (new)
- `services/github-review/src/worker.py`
- `services/github-review/src/api.py`
- `services/github-review/src/providers/claude.py` (one comment)
- `services/github-review/tests/test_provider_chain.py` (new)
- `services/github-review/tests/test_provider_failover.py`
- `services/github-review/tests/test_quota_rank.py`
- `services/github-review/tests/test_api.py`
- `services/github-review/tests/test_concurrency.py`

## Approval

**Needs your yes** — this repo's `AGENTS.md` has no `## Approval` section, so nothing is pre-approved. The gate question is whether to move the provider policy out of `ReviewWorker` into its own directly tested module, as a pure refactor. Recommended answer: yes, because the policy is the part of the reviewer that has grown in each of the last three features, and it is plain decision logic that today can only be exercised by standing up the whole service around it.

No path here matches a hard stop, and nothing moves the credential line or changes a role interface: no config key, no `/status` field, no posted text and no stored column changes.

## Out of scope

- Any behaviour change: the failover rules, the ranking formula, the banner wording, the log lines and the `/status` payload all stay as they are.
- Splitting the rest of the lifecycle (orphan recovery, approval carry-over, budget caps) out of `worker.py`.
- The `/status` shape contract (`review-status-contract`).
- The behaviour of `providers/claude.py`, `providers/codex.py` and `quota_rank.py`, which are already separate and directly tested.
