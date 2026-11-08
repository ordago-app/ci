# Conversational review loop

**Priority:** low

Moved from homelab on 2026-11-08, where github-review was developed until the two copies
were reconciled here. Rechecked against this repo's `services/github-review` that day.

**Goal:** Let an opener (human or agent) and the automated reviewer iterate on a PR
through *messages*, not just commits — exchanging replies until the reviewer
approves or the two reach an agreement.

## Context

Today the reviewer already supports a **commit-driven** loop: each new head SHA on a
PR triggers a fresh review round (the 60 s poll, or a `POST /reviews` enqueue), bounded
by the round cap in `services/github-review/src/worker.py`. The natural flow works:

1. opener pushes `SHA-1` → CI green → reviewer reviews `SHA-1`, posts a verdict +
   findings bound to `commit_id=SHA-1`;
2. opener pushes a fix `SHA-2` → reviewer reviews `SHA-2` afresh (not idempotent-
   skipped — `get_posted` keys on head_sha);
3. repeat until `VERDICT: APPROVE` or the round cap.

What does **not** exist yet is a *conversational* loop. Three gaps, all in the same
area. (One that used to be here is closed: since homelab#271 a later round gets the
previous review's text and the diff pushed since, through `PreviousRound` in
`prompt.py`.)

- **No replies.** `build_review_prompt` (`services/github-review/src/prompt.py`) sees
  its own previous review but never the opener's replies. A finding "clears" only
  because the diff changed so it no longer applies, not because the reviewer agreed.
- **No persuasion path.** The opener cannot argue "this is intentional because X"
  and have it considered: review-thread comments are never fed to the reviewer.
- **No comment trigger.** A re-review fires on a new head SHA or the timer. A reply
  with no commit produces no new round.

This idea adds the conversational layer. It is **additive** — nothing in the
current architecture blocks it.

## Design / approach

Three pieces. Piece 1 (conversation-aware prompt) is independently shippable.
Pieces 2 and 3 are **coupled**: comment-driven re-review at an unchanged head SHA
(Option B below) is impossible under today's dedup model and requires the storage
change described in Piece 3 to land first.

### 1. Conversation-aware prompt

Extend `build_review_prompt` (which already carries the previous review) to include:
- PR review-thread comments and issue comments posted *since* the last review,
  attributed by author (so the reviewer can tell the opener's reply from its own
  prior text).

Bound the history to keep the prompt sane (e.g. last N rounds, or last K comments,
or a token budget). The reviewer is then told explicitly: "you may clear a prior
finding if the reply convincingly resolves it; otherwise restate it."

### 2. Comment-driven re-review trigger

Add a path so a *reply* (not just a commit) can start a round:
- Option A — **explicit:** opener (or a maintainer) posts a recognized marker
  (e.g. `@reviewer take another look`, or re-applies the `ai-review` label) and a
  GitHub webhook / the poll picks it up.
- Option B — **poll diff:** the poll already runs every 60 s; have it
  enqueue a new round when there are unaddressed opener comments newer than the
  last review, even if the head SHA is unchanged.

**Storage prerequisite for Option B.** Today review identity is keyed on
`(repo, pr_number, head_sha)`: the `review_jobs` table's `UNIQUE` key, which
`ReviewJobStore.enqueue` (`services/github-review/src/job_store.py`) relies on, so a
second round at the same head SHA is deduped away and never runs. Re-reviewing at an unchanged head (the
whole point of Option B) therefore requires relaxing that model first: add a round
ordinal (or a comment cursor) to the review identity so each round is a distinct
row, and have idempotency key on `(repo, pr_number, head_sha, round)` instead. This
is the Piece 3 storage change; Option B cannot ship without it. Option A, by
contrast, can drive a re-review by changing the head (a `synchronize`-style event)
or by treating the marker as a fresh job under a relaxed key — same prerequisite if
the head is unchanged.

Option B reuses existing machinery (no webhook ingress) at the cost of up-to-60 s
latency; Option A is snappier but needs an event source. Likely B first, A later —
both gated on the Piece 3 storage change.

### 3. Round/termination accounting

Today the round cap (`_budget_spent` in `worker.py`) counts reviews per PR, and review
identity is keyed on `(repo, pr_number, head_sha)` (see the storage prerequisite
above). A back-and-forth negotiation will burn rounds faster than commit-only
iteration and needs to round-trip at an unchanged head, so:
- **relax the dedup key** to include a round ordinal so multiple rounds can exist
  for one head SHA (the prerequisite that unblocks Piece 2 Option B);
- raise / make-configurable the cap for conversational PRs;
- define the **stop conditions** precisely (see open questions) so an opener and
  reviewer can't argue forever.

## Open questions

- **How much thread history** does the reviewer get — last N rounds, a token
  budget, or the whole thread? Cost vs. context-completeness tradeoff.
- **Can a reply alone clear a finding**, or must every cleared finding correspond
  to a diff change? (Letting argument-alone clear findings is the whole point of
  "reach an agreement," but it weakens the test-coverage gate, which is currently
  a hard rule in the prompt.)
- **Infinite-argument stop condition.** Beyond the numeric round cap: do we
  detect "no new information in the last reply" and force a terminal verdict?
  Who breaks a tie — does the reviewer get the final say, or does it escalate to
  a human with `escalated=true`?
- **Trigger choice** — poll-diff (Option B, no ingress) vs. an event/webhook
  (Option A). Does the no-ingress security invariant (reviewer reachable only over
  the internal docker network) survive Option A?
- **Identity** — when the opener is itself an agent, do we need any loop-breaker
  to stop two bots from ping-ponging indefinitely on cost grounds?
- **Cost ceiling.** Each round is a full Codex review session. What's the per-PR
  budget, and does it tie into the same accounting as `max_rounds`?
