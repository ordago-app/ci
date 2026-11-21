---
name: advance-plans
description: Use when the user wants parallel agents to build and land approved plans until nothing agents can move is left — "push the plans", "work the backlog", "build what's approved", "drain the plans map", "advance/retire plans this cycle", "is everything for this release done" — or when docs/plans/ has approved work and capacity is free.
---

# Advance plans — build what was approved, until the map drains

**Everything in this skill's pool is already approved** (the repo's `AGENTS.md` `## Approval`; a repo without that section has no pre-approved ideas, and its pool is `ongoing/` and `ready/` only): a plan in `ongoing/` was started, one in `ready/` got a yes, and a pre-approved idea needs none. So there is **no `go`** — the leader triages, dispatches and reports. Whether a plan is still *true* is checked **once, by the worker who builds it** (§4 step 1): plan facts rot in weeks, and a fit review a cycle ahead of the build is a check the build has to redo.

**REQUIRED SUB-SKILL:** run `orchestrate` for everything mechanical — naming, `batch.json`, admission, dispatch, supervision, landing, the escalation contract, wind-down, and the repo facts it reads (`<project>`, `<base>`, `<wt-dir>`). This skill replaces its Phase 1 selection and `go`, and sets the finish line.

**The goal is a state of the map, not a set of PRs:** drained means no plan under `docs/plans/ongoing/` (incl. `soak/` if the repo has one) reads `Gate: none` on `origin/<base>` except those **in flight elsewhere** (§1), no startable plan in any pool is unassigned, and no worker is non-terminal. Every remaining plan is then waiting on something agents cannot do now — and says what.

**The user's inbound traffic is exactly:** the first message (§3), the questions `orchestrate`'s escalation contract allows (chiefly product questions workers raise, §4), the unlock queue, and the drained notice (§6). Never end a message with "reply go", and never hold a dispatch waiting on an answer.

## Launch

```bash
claude -n <project>-build --settings '{"crossSessionInbound":"accept"}' "advance the plans"
```

Started some other way? Ask once for `/rename <project>-build` and the `/config` setting (`orchestrate` Phase 0), but dispatch regardless — the `reports/*.log` channel carries the workers until the name is fixed. Dispatched by another session that is the user's interface? Send every message in this skill to that session (`orchestrate` § decision routing), not to your own window.

## 1. Pool and triage — from the files, mechanically

**Three pools, in this order:**

1. **`ongoing/` and `ongoing/soak/`** — started work. An in-flight plan that waits a cycle is debt carried every cycle: if it can advance, it does.
2. **`ready/`** — approved, not started.
3. **Pre-approved ideas** — triaged with the rest and queued behind pools 1–2, so they run as soon as those leave capacity free. An idea counts when a `review-ideas` run classed it PRE-APPROVED and it is still in `ideas/`:
   ```bash
   git log origin/<base> --format='%(trailers:key=Pre-Approved,valueonly)' | sort -u \
     | while read -r s; do [ -n "$s" ] && git cat-file -e "origin/<base>:docs/plans/ideas/$s.md" 2>/dev/null && echo "$s"; done; true
   ```
   Beyond that list you may class ideas yourself from block and opening paragraph against the repo's `## Approval` — `high` first, oldest `Advanced` first within a priority, about as many as the queue could use this batch. **Unsure which class → leave it** for `review-ideas`; it needs a yes, and asking is that skill's job. A classed idea still goes through the table below: a pre-approved idea can be Visual, human-only or in conflict like any plan.

**Find who else is working first.** A plan someone else is building is not yours to triage, gate or dispatch, and its block belongs to its owner. *In flight elsewhere* means any of:

- an open PR touches the plan's paths or names its slug — skip a **draft with no push in 7+ days**: it is parked, not live; list it in the message so the user can close it;
- a worktree (`git worktree list`) whose branch has commits or uncommitted changes against `origin/<base>` touching those paths — `git -C <wt> diff --name-only origin/<base>...HEAD` plus `git -C <wt> status --porcelain`; an unpushed branch counts, and so does a plan file that exists only on that branch;
- a **live interactive session** pushing to `<base>` directly — commits on `origin/<base>` in the last few hours that touch the plan's paths, from no batch you know of. Every session commits as the same human, so ask the sessions `ListAgents` shows before you dispatch into those files, and cut the worker into a worktree either way;
- a worker of a **live** leader — a `~/.claude/orchestrator/*/batch.json` whose non-terminal worker still has a tmux window (`tmux list-windows -a -F '#S:#W'`) or a session in `ListAgents`. A state file alone proves nothing: old ones keep non-terminal workers for months.

**Read the blocks on `origin/<base>`** (`git fetch origin <base>`, then `git show origin/<base>:<plan>`), never the main checkout. The committed map lags them until CI regenerates it; use it only for what it derives — `Advanced` and ⚠️ — and never regenerate it locally.

**Put every plan — or every slice of a mixed plan — in exactly one bucket.** The leader does not judge whether a plan is still true or worth it — the worker does — so every test here is observable without reading the code:

| Bucket | Observable test | Action |
|---|---|---|
| **in flight elsewhere** | the test above | touch nothing — not its block, not its gate; list it with its owner (PR, worktree, leader) |
| **startable** | `Gate: none` (or none written), and an agent can do the work from a worktree; the paths the plan names intersect no open PR (`gh pr list --state open --limit 200 --json number,files`) and no live worktree's diff — a PR that only edits the plan doc is a coordination note, not a conflict | queue it |
| **fired** | a `release:`/`soak:`/`blocked:` trigger has happened — the version cut *and* live in prod for a prod gate, a force-update floor raised, the soak date passed, the awaited plan or PR landed | set `Gate: none` (and fix `Landed`/`Next`), then re-bucket |
| **not now** | waits on an unlanded plan or PR, a version on the fleet, or a conflicting diff | write a `**Gate:**` — `blocked:<plan slug or PR #n>`, `release:<x.y.z>` — so the next batch does not re-sort it |
| **misfiled** | a `ready/` plan a merged PR already worked — `gh pr list --state merged --search <slug> --limit 50 --json number,files`, counting a PR whose files include the plan path *and* something outside `docs/` | `git mv` to `ongoing/` with its true `Landed`, and treat what remains as startable **in this batch** — a leader that already triaged would otherwise leave it orphaned |
| **needs your go** | the next step is a write or deploy the repo's `AGENTS.md` reserves for the user (always production), a hard-stop merge (a `hardStop` path, `Breaking-Client:`, a release PR), or a `decision:` gate. What `## Approval` frees — in some repos dev and beta writes and deploys — is not an unlock; the worker runs it | the unlock queue (§3); a code half, if any, is startable too |
| **visual** | the repo's `AGENTS.md` routes it to the user's own session (e.g. app UI tuned by eye on a device) | list it for the user — main checkout, never a worker, never the leader. Unclear whether it qualifies: ask |
| **human-only** | needs a phone, a console UI, a store, another repo | set `Gate: blocked:<the human step>` and list it |
| **gated** | the trigger is genuinely in the future | leave it |

**Check a trigger against reality**, never against the plan's prose: `gh`, `git log`, and read-only reads of the live system (a log query, a `SELECT` against a data mart or the database). When the trigger may have fired but proving it is itself the work — read the mart, then retire — bucket it **startable**: verification-only work is a worker's, not the leader's. A trigger only a console UI can show (Crashlytics, a store console) makes that step **human-only**.

**A plan with mixed slices** — a worker slice, a Visual slice, a gated slice, a needs-your-go slice — has each slice bucketed on its own. Working any slice starts the plan; the others become its `Next` or its gate. A needs-your-go slice that does not depend on the plan's gate goes in the unlock queue now. A conflicting slice holds only itself if the rest is useful alone (the `## Approval` mixed-plan rule); otherwise it holds the plan.

**Queue order:** pool first (above), then within a pool: how many other plans it unblocks — counting plans whose `Gate` names it, and `blocked:` gates whose blocker it would remove (a plan waiting on green runs of a flaky lane is unblocked by a plan that fixes the lane) — > `Priority` > ⚠️ stale > smallest remaining work. Priority orders; it never excludes — an approved `low` plan runs when nothing above it is waiting. One plan per worker; never bundle plans to save slots.

**The conflict check is coarse here and exact later.** An approved plan names few files — approve early, plan late — so triage checks the paths it does name; the worker reports its real file list before it builds (§4 step 2), and the leader checks that list against every other worker and open PR and answers *go* or *wait for #n*.

## 2. Fix the blocks that lie — before the first dispatch

Every correction from triage goes out as **one** Direct docs commit to `<base>` — the whole block, not just `Gate`: a fired gate usually leaves `Next` stale, and a merged step leaves `Landed` behind. A row reading `Gate: none` that no agent can move makes the drained state unreachable; a fired gate left closed hides work.

- Waiting on another plan or an open PR → `blocked:<plan slug or PR #n>`.
- A vague soak ("days", "a while") → a dated or observable trigger when the plan's own reasoning supports one; otherwise `decision:<the question>`.
- `not now` gates, `human-only` gates and `misfiled` moves join the same commit; fold in the first dispatches' moves (§3) when they happen in the same minute.

Write it from a detached docs worktree at `origin/<base>` named for you (`<wt-dir>/<your session name>-docs`; another leader may hold any shared name), never the main checkout. Run `git submodule update --init` in it after every checkout: the validator is a symlink into the submodule, so without it the validator is missing, and the failure looks like a broken plan. `node scripts/plans-map.js --validate; V=$?` before every push — never through a pipe — and never commit a regenerated map.

## 3. Dispatch by capacity, then one message

- **Admit, don't size** (`orchestrate` Phase 2): before each dispatch `pnpm agent:capacity` must admit; while it says no, the plan waits in `batch.json`'s queue. Re-check whenever a worker enters CI-wait — it holds no local capacity, which is when the 2026-10-02 batch profitably started two more.
- **Move the plan when its worker starts**, as a Direct docs commit: `git mv` from `ready/` (or straight from `ideas/`) to `ongoing/`, with the full block — `Landed:` the furthest env any slice reached (usually `none`), `Gate: none`, `Next: verify the plan against <base>, then build it`; `Priority` kept. A plan already in `ongoing/` keeps its block unless §2 corrected it. Then `pnpm agent:dispatch`. A plan worked where `ongoing/` readers cannot see it is invisible to every other leader.
- After the first dispatches, send the user **one** message:
  - **Dispatched / queued** — one line each, informational;
  - **Gates written** — one line each;
  - **Unlocks** — every *needs your go* item, ranked by how many plans it unblocks, as `orchestrate`'s **Needs your decision** block, each recorded in `needsUser`. The user answers any subset, whenever; the leader runs each approved one **itself, dry run first** (`orchestrate` Phase 2), logs every command and count in `batch.json`, and messages the affected worker. An unanswered unlock blocks nothing but its own step;
  - **Visual** and **human-only** lists, one line each with the step;
  - in the same decision block, only if there is one: an unclear-Visual call, or a product question the plan text already states — even one the plan says to settle "before building": ask it here, dispatch anyway, and the worker builds what does not depend on the answer;
  - **in flight elsewhere** — one line each with its owner, and any parked draft PR that held a plan back.

## 4. Worker prompt — add this block to `orchestrate`'s template

`orchestrate`'s template plus this block, plus `orchestrate`'s **plan finish line** block:

```
Your plan is APPROVED — the user or AGENTS.md ## Approval said it should
exist. Whether it is still TRUE is your first job, and nobody has checked
it since it was approved:
1. Verify every load-bearing claim against origin/<base>. A bug claim (a
   race, a double write) needs the guard checked at EVERY layer — screen,
   service transaction, rules — before you believe it. A plan already in
   ongoing/: check its Landed / Gate / Next against reality too (a step it
   names may already be merged). Then check the approval covers what you
   are about to build. A plan in ready/ or ongoing/ is approved for what it
   states; a plan from ideas/ only if it is PRE-APPROVED by AGENTS.md
   ## Approval as a whole. A step beyond that — one that changes what a user
   sees or can do, a rule, stored data or running cost, and that the plan
   does not state — stops, and goes up as NEEDS-YES. Report one of:
     RETIRE <plan> — shipped: <code path + merged SHA, live on prod for runtime work>
     DEMOTE <plan> — <what is no longer true, with evidence>
     NEEDS-YES <plan> — <the ## Approval line it hits, and the question>
2. If the work needs a PR: write the implementation plan (files, tasks) for
   today's code in your PR description — not in docs/plans/, which the
   leader owns — and SEND THE LEADER YOUR FILE LIST before you build; wait
   for its go (it checks that list against the other workers and open PRs).
   Every later PR in the loop: send its file list the same way. Work that is
   only verification (read a mart, a log, then retire) needs no PR: go
   straight to your finish-line report, with the queries as evidence.
3. Technical choices are yours: pick, and say why in the PR. A PRODUCT
   question — what a user sees or can do, a rule, a migration, a cost — goes
   to the leader; keep working on whatever does not depend on the answer.
```

## 5. On each terminal report

1. **RETIRE / DEMOTE from step 1:** re-check the evidence yourself, then `git rm` the plan (or `git mv` it to `ideas/`, saying in it what changed) as a Direct docs commit. That is a correct outcome, not a failed worker — the check ran at the right moment.
2. **NEEDS-YES from step 1:** `git mv` the plan back to `ideas/` and put the worker's question in the **Needs your decision** block — it is now verified, which is exactly what a yes should rest on. On yes, write the answer into the plan, move it to `ready/` and queue it.
3. **RETIRE / BLOCK at the finish line:** reject a report that cannot drain the cycle — `Gate: none` with work left, a vague gate, a `RETIRE` of runtime work with no prod evidence ("merged is not verified") — and send it back. Otherwise write the block, or `git rm` the plan (default; extract to `docs/decisions/` only per `managing-plans-lifecycle`), as a Direct docs commit. Workers never touch `docs/plans/**`, so this never conflicts with their PRs.
4. Dispatch the next queued plan into the free capacity. Re-triage only rows a landed PR could have changed.

## 6. Drained → notify once

When the drained state holds — read the plan files on `origin/<base>`, the map regenerates later — send **one** message (and a `PushNotification`): "Everything agents can advance is done", then:

- what landed, and what was retired or demoted at step 1 and why;
- the ongoing plans grouped by what they wait on — **you** (unlocks and decisions pending or declined), **a human step**, **release x.y.z**, **soak trigger**;
- the visual / human-only lists still waiting;
- when the approved pool ran low: one line offering a `review-ideas` run, which turns ideas into approvals. Do not start it uninvited.

Then `orchestrate` Phase 4.

## Common mistakes

| Mistake | Instead |
|---|---|
| Asking for a `go` | Everything in the pool is approved; dispatch, and report |
| The leader re-verifying every plan before dispatch | The worker verifies, once, when it builds |
| Writing File Structure / Tasks into the plan file ahead of time | The worker writes them for that day's code, in its PR |
| Treating a step-1 RETIRE / DEMOTE / NEEDS-YES as a failure | It is the check doing its job; apply it and refill the slot |
| Gating or re-blocking a plan another PR, worktree or leader is building | It is in flight elsewhere: touch nothing, list it with its owner |
| Reading an old `batch.json` as a live leader | Live means a tmux window or a `ListAgents` session; state files outlive their leaders by months |
| Working `ready/` while an `ongoing/` plan could advance | Pool order: started work first |
| Skipping a `low` plan | Priority orders the queue; it never excludes |
| Classing an idea pre-approved on a hunch | Unsure → leave it for `review-ideas` |
| Starting work with the plan still in `ready/` or `ideas/` | `git mv` to `ongoing/` with a block at dispatch |
| Treating "all workers terminal" as done | Done is the map state; refill until no startable plan is unassigned |
| Rejecting a plan and leaving its `Gate: none` | Re-gate it (`blocked:<human step>`), or the cycle never drains |
| Skipping soak/blocked/release rows as "not pickable" | Check whether the trigger fired; many have |
| A worker stopping at a subset with `Gate: none` left | The loop runs until the next step is out of reach |
| Leaving a *not now* in prose | A `**Gate:**` in the plan, or the next batch re-sorts it |
| Filing data ops under "rejected" | They are unlocks — queue them ranked by plans freed |
| A fixed worker count | `pnpm agent:capacity` before every dispatch |
| Handing a Visual plan to a worker | List it: main checkout, the user's session |
| A worker asking the leader a technical question | It decides and explains; only product questions go up |
| Taking the user's answers as covering each beta/prod `--apply` | The permission classifier asks again per command, even under standing authorization (2026-09-14: 4 of 7 applies bounced). Dry-run and back up first, then put the exact `--apply` commands to the user in one message |
| Trusting a plan's "backup and restore are ready" before a destructive run | Check the script writes one. `medals-showcase-reconcile` did not; dump every doc the dry run lists before `--apply` |
| Hand-merging a PR once "every non-reviewer check is green" | Also confirm any change-detection job ran and the lanes it gates reported on the current head. A queued detector means the gated lane never ran |
| Merging a green PR whose base moved underneath it | Diff the base's changes against the PR's files first; an intersection means rebase. One such rebase exposed a seed a sibling PR had added with the very field the first PR removed |
| Relaying a worker's diagnosis of a red lane or a queue ("runner dead", "lane down") | Check the jobs API yourself: three such reports in one batch were saturation, a lagging checks API, or a documented false error message |
| Running `review-ideas` because the pool is thin | Offer it in one line; the user starts it |
