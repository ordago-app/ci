---
name: ship-a-feature
description: The end-to-end autonomous delivery contract — front-load every business and technical question into ONE message, get one decision, then implement in a worktree and land the PR without further check-ins. Use for any feature request, refactor, or change that is not a docs-only edit. SUPERSEDES `superpowers:brainstorming`'s one-question-per-message rule and `superpowers:finishing-a-development-branch`'s stop-and-ask merge menu — do not run those flows in a repo that adopts this skill. For defects use the repo's `fix-bug` skill (same landing step, different entry point).
---

# Ship a Feature

The user is a decider, not a merge gate. A change should cost them **two messages**:
their request, and one decision.

**Announce at start:** "Using ship-a-feature — I'll come back with one decision message."

> **Shared skill.** Consumed by several repos via the `agent-skills` submodule. Every
> repo-specific value — the Direct-path list, the hard-stop list, the base branch, whether
> an approving review is required — lives in that repo's `AGENTS.md` and in
> `.agents/land.config.json`. Where they differ from this file, they win.

## Step 0 — Classify

| Diff touches | Mode |
|---|---|
| *Only* the repo's **Direct paths** (docs, markdown, agent config — see `AGENTS.md`) | **Direct** — commit and push to the base branch. No PR, no worktree, no asking. Stop reading here. |
| Anything else | **Autonomous** — continue below. |

Never ask "Direct or Isolated?". Classify from the diff you are about to make.

## Step 1 — Silent recon

Read the code first. Do not ask anything yet. You cannot write good options without
knowing what exists — half of a typical question list answers itself in the repo.

## Step 2 — ONE message

Send exactly one message, containing all of:

**Understanding** — 2–3 lines restating the ask.

**Business decisions** — numbered. Every open product question, each with its options,
**your pick**, and why. Business comes first because it changes the technical shape. Do
not defer a product question to "we'll see during implementation" — that is how a second
message gets born.

**Technical options** — 2–3 approaches, ranked by *long-term codebase shape*, not by
effort. Mark the recommended one and say why it wins **long-term**. If you are preferring
an option because it is less work now, you have ranked them wrong.

**Blast radius** — does this hit the hard-stop list (§5)? Need a migration or backfill? A
regression test? A schema or infrastructure deploy?

**Default** — end with: *"Say `go` and I take all my picks."*

### The one-message rule is hard

No second question message. If something blocking surfaces mid-implementation:

1. Finish everything that does **not** depend on the answer.
2. Then batch a single follow-up, stating what you assumed in the meantime and what you
   already built under that assumption.

A question you could have answered by reading the code is a bug in Step 1.

## Step 3 — Implement

On `go` (or an explicit choice), start immediately. No "shall I begin?".

- Work in a **git worktree**, branched from the repo's base branch.
- Test-first, via the repo's TDD skill.
- In a worktree the PR is the review surface, so commit freely — do not wait for the user.
- **Push once.** Commit locally as often as you like; push when the branch is ready. Where
  CI sets `cancel-in-progress`, every intermediate push burns a runner slot and cancels its
  predecessor's run.

## Step 4 — Land

```bash
pnpm pr:land
```

A resumable state machine, not a merge command. Run it, act on the exit code, run it again:

| Exit | Meaning | You do |
|---|---|---|
| `0` | Merged, branch deleted | Done — go to Step 5 |
| `10` | CI red | Read the log, fix, re-run |
| `20` | Review requested changes | Fix the **cause**, never silence it, re-run |
| `30` | Hard-stop, no reviewer, or rounds exhausted | **Stop.** Hand to the user with the PR link |
| `40` | Preflight failed — dirty tree, protected branch, or the PR conflicts with its base | Resolve, re-run |

Exit `20` is a budget, capped at `maxReviewRounds`. What happens at the cap is the repo's
call, in `roundsExhausted`: `"handoff"` turns it into exit `30`, and `"merge"` ends the
review conversation and lands the PR on CI green. Under `"merge"` there is nothing left to
ask the user — do not stop to report that the rounds ran out. Neither setting relaxes the
hard-stop gate.

A PR that **conflicts with its base** is exit `40`, reported as a conflict. It is worth
knowing why it is not a timeout: `pull_request` jobs check out `refs/pull/<n>/merge`, and
GitHub cannot build that ref while the merge conflicts — so a conflicting PR dispatches no
workflow runs at all, and waiting for CI on one waits forever. Rebase onto the base,
resolve, push with `--force-with-lease`, re-run.

On exit `10`, read the failure before assuming it is your code. An infrastructure failure
— a broken runner cache, a hung service lane — is not a regression to "fix". Inventing a
code change to satisfy broken infrastructure is the single worst failure mode of this
contract.

Never pass a hard-stop override flag. If `pr:land` says a human decides, a human decides.

## Step 5 — Notify once

Report completion in one message: what landed, the PR number, anything you assumed, and
anything you deliberately left out. Then stop.

Do **not** report progress mid-flight. Silence between Step 2 and Step 5 is the product.

## 5. The hard-stop list

Changes whose blast radius outlives a green test run never self-merge, however green.
**The authoritative list for each repo is in its `AGENTS.md` and enforced in
`.agents/land.config.json`.** The recurring shapes:

- Security rules and access-control config
- Index / schema definitions that require a deploy
- Removing or tightening a published API or callable
- Converters and validators that tighten an already-stored shape
- Data backfills and any administrative write
- Anything declaring a breaking change for already-installed clients
- Any deploy
- **Any PR targeting a release branch** — release flows keep their human gate entirely

"Green" answers *did the tests pass*, not *is the blast radius acceptable*. These are the
changes where those two questions diverge.

## Repos without a reviewer

This contract replaces human review with machine review — it does not remove review. In a
repo with no automated reviewer configured, `requireApprovingReview` is `false` and the
merge bar is CI alone, which is a **weaker** posture, not an equivalent one. Say so plainly
in Step 2's blast-radius line when it applies, so the user can decide whether that repo's
test suite is genuinely a sufficient gate for this change.

## What this supersedes

`superpowers:brainstorming` mandates one question per message and a hard approval gate
before any implementation. `superpowers:finishing-a-development-branch` mandates stopping to
offer a 3-option merge menu. **Both are overridden** in repos that adopt this skill —
per-repo instructions outrank skills, and the adopting repo's `AGENTS.md` names the override
explicitly.

Keep using `superpowers:systematic-debugging`, the repo's TDD skill, and its domain skills —
this skill replaces the *intake and integration* ceremony, not the engineering rigour.

## Red flags

| Thought | Reality |
|---|---|
| "I'll just quickly confirm one thing first" | That is the second message. Read the code or state an assumption. |
| "I'll check in at the halfway point" | Silence is the product. Report at the end. |
| "CI is red, let me adjust the code until it passes" | Read the log first. Infra failures are not regressions. |
| "It's green and approved, the rules change can merge" | Hard-stop paths never self-merge, however green. |
| "I'll ask which mode they want" | Classify from the diff. Step 0. |
| "The user gave a vague `go`, so I'll ask for detail" | `go` means *take all your picks*. Take them. |
| "No reviewer here, so green CI is the same bar" | It is a weaker bar. Name that in Step 2. |
