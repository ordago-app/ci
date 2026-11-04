# github-review: a restart strands the job it interrupted

**Priority:** medium

`ReviewJobStore.list_retryable` selects `queued` and retryable `failed` jobs, and
nothing moves a `running` row anywhere else. A service restart during a review,
which every deploy can cause, leaves that row `running` for good. The poller's
`INSERT OR IGNORE` then treats the head as already handled, so it is never
reviewed again unless someone pushes a new head. On a repo where the review is the
merge gate, that PR waits with nothing saying why.

Fix: on startup, before the first tick, mark `running` rows failed with a reason
("interrupted by restart"), so the normal retry picks them up. The worker runs one
job at a time in one process, so at startup every `running` row is stale.

`ClaudeReviewProvider.reap_orphans` already removes that provider's leftover
container at startup. Codex containers have no equivalent and should get one in the
same change, since each restart can leak one.
