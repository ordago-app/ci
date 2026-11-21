---
name: orchestrate
description: Run this session as the batch orchestrator (leader) for parallel worker Claude sessions — the engine under advance-plans, and on its own for an ad-hoc batch. Use when the user says "orchestrate", "start a batch", "run these in parallel", or wants one central agent to supervise multiple worker sessions to landed PRs. The leader admits workers by measured capacity, dispatches them into tmux windows + worktrees, steers via SendMessage, keeps durable batch state, and escalates only hard-stops, blocked workers, product questions and scope changes. Building approved plans → advance-plans; checking ideas → review-ideas. Not for single-task work — use ship-a-feature or fix-bug directly for that.
---

# Orchestrate — leader contract for parallel worker sessions

You are the **leader**. Your job is: choose the batch with the user (one decision), dispatch and supervise workers, keep the batch state truthful, and bother the user only when the contract says so. You do NOT implement tasks yourself — a leader with its hands in a worktree stops supervising.

Everything below was measured on real batches (the first, on 2026-08-22: two leaders, ten workers, ten merged PRs). Where a rule cites a failure, that failure happened.

## Repo facts this skill reads — never guess them

This file is procedure. Every project fact lives in the consuming repo, which wins wherever it differs:

| Fact | Where |
|---|---|
| `project` (names everything below), `maxConcurrentEmulatorSuites`, `worktreesDir`, `worktreeSetup`, `capacity`, `ciCapacity` | `.agents/orchestrate.config.json` |
| base branch, `hardStop` patterns, `reviewLabel`, `requireApprovingReview`, `maxReviewRounds`, `roundsExhausted` | `.agents/land.config.json` |
| what agents may start without asking (`## Approval`), forbidden operations, which envs are protected, PR-body requirements, plan conventions | the repo's `AGENTS.md` |

Names derived from `project`: leader session `<project>-orchestrator`, plan-builder leader `<project>-build` (see `advance-plans`), tmux session `<project>-fleet`. Below, `<base>` is the land config's `baseBranch` and `<wt-dir>` is `worktreesDir`. Every command a leader or worker runs is in the consuming repo, where the shared scripts are linked into `scripts/` (`pnpm agent:capacity`, `pnpm agent:dispatch`, `scripts/pr-land-bg.sh`).

**If `.agents/orchestrate.config.json` is missing, stop** and tell the user the repo is not wired for orchestration (the agent-skills README lists what it needs). Do not improvise the names — two leaders guessing different ones is how state gets clobbered.

## The prime rule: update state, then act

`~/.claude/orchestrator/<project>-<leaderSession>/batch.json` is the single source of truth — not your context. **Before and after every action** (dispatch, received worker report, escalation, landing), update it.

**One state directory per leader, never one per repo.** Two leaders ran in the same repo on the same day and the second clobbered the first's `batch.json` within minutes of it going live. Derive the path from your own session name and never write another leader's directory.

**`batch.json` is coordination state, not a record of repo debt.** Anything a future agent needs — a finding, a stale doc, a deferred fix — goes in `docs/plans/` or `docs/{decisions,incidents,ops}/` per `managing-plans-lifecycle`. A leader that records its own findings in `batch.json` and calls them durable has broken the rule it spends all day enforcing on its workers; the failure is invisible from inside the leader role, which is why it is written here.

**No dashboard artifact.** A previous revision of this skill maintained a rendered HTML dashboard published as an Artifact. It was dropped: the user's verdict was that it added nothing over the terminal, it cost a render-and-publish on every state change, it drifted from reality whenever the leader forgot a step, and a deleted artifact makes its file path permanently unpublishable. The terminal is the decision inlet; `batch.json` is the state; the wind-down summary is the report.

### batch.json schema

```jsonc
{
  "batchId": "2026-08-22-a",            // date + letter; names the batch in reports and commits
  "started": "2026-08-22T10:00:00Z",
  "leaderSession": "<project>-orchestrator",
  "needsUser": [                        // ONLY items matching the escalation contract
    { "when": "…", "what": "…", "worker": "…" }
  ],
  "decisions": [                        // every user decision, verbatim enough to replay
    { "when": "…", "asked": "…", "answer": "…" }
  ],
  "workers": [
    {
      "name": "w1-fix-roster",          // wN-<short-slug>; also the tmux window name and the worker's -n name
      "plan": "docs/plans/ongoing/….md", // or "ad-hoc: <one-line>"
      "branch": "fix/roster-dedup",
      "worktree": "<wt-dir>/w1-fix-roster",
      "window": "<project>-fleet:w1-fix-roster",
      "slot": 1,                        // from agent-env.sh, filled after the worker reports it
      "status": "dispatched",           // pending|dispatched|working|pr-open|review-round-N|blocked|landed|failed|aborted
      "pr": null,
      "lastReport": "2026-08-22T10:05:00Z",
      "notes": "…"                      // last milestone, terse
    }
  ]
}
```

## Phase 0 — start or resume

1. **Name yourself first.** Run `/rename <project>-orchestrator`. Session names are pid-derived, so a crash or restart silently renames you and **every worker's dispatch address goes stale at once**. Workers do not fail loudly when that happens — they keep working and go blind. A stable name is the only cheap defence. **The leader cannot run `/rename` itself** — the cheapest fix is the user launching it pre-named, `claude -n <name> --settings '{"crossSessionInbound":"accept"}'`, which also covers step 2; otherwise ask the user, then confirm with `ListAgents` (its first line prints your own name) *before* writing the address into a single worker prompt. On 2026-09-08 the user renamed to a different name than asked, and again after a restart, so every worker's first reports went to the log file only; the `reports/*.log` channel is what made that survivable. Keep the state directory under the name you started with — it is a path, not an address — and re-announce the new name to every live worker after any change.
2. Enable inbound peer messages: `/config` → *Messages from your other sessions* → **accept** (`crossSessionInbound`). Messages are **held and expire** by default when the sender's permission mode differs from yours, which it always will — workers run unattended. Without this, every worker report silently expires.
3. `cat ~/.claude/orchestrator/<project>-<you>/batch.json` — **if it exists with non-terminal workers, you are resuming**: reconcile against reality (`gh pr list --base <base> --state all`, `git worktree list`, `ListAgents`), update statuses, and give the user a 3-line "here's where the batch stands" before anything else. The previous leader's transcript is optional colour; the file plus GitHub is authoritative.
4. **On resume, re-announce your address to every live worker** — do not wait to be missed. A resumed leader that stays quiet has a fleet working blind.

## Phase 1 — what the batch works on

**Route by pool first; this phase is only for an ad-hoc batch.**

- Building plans from `docs/plans/` (`ongoing/`, `ready/`, pre-approved ideas) → **`advance-plans`** on top of this skill. Everything in its pool is already approved (the repo's `AGENTS.md` `## Approval`), so it replaces this phase's selection and `go`: it dispatches on its own and asks only what the escalation contract allows.
- Checking `ideas/` against the code → **`review-ideas`**. Not an `orchestrate` batch at all: no PRs, no workers, in-session scouts.
- Anything else — a list the user names, live requests, a mix — → the steps below.

1. **Read [`docs/plans/_plans-map.md`](../../../docs/plans/_plans-map.md) first** — it is the generated index of every in-flight plan with its priority, how far it landed, what gate holds it, and its next action. **Actionable now** is the candidate list; the release sections are not pickable this cycle; ⚠️ marks what has not advanced in 2+ cycles, which is usually where the real backlog is hiding. Only open the individual plans you shortlist. Add anything the user asked for live; check `docs/plans/ideas/` only if the user invites it.
2. **Know what each stage vouches for.** `ready/` is *approved, not verified*: its decision holds, its facts may not — the worker who builds it verifies them first (`advance-plans` §4 step 1 is the block to inline). `ideas/` is an unswept inbox, and an unapproved one: diff a pick against `<base>` before it reaches the user's message at all, since you are asking for a yes on its facts. Measured: four of five `ideas/` picks were stale and three were already fully shipped; on 2026-10-03 a `high` `ready/` plan described a bug that could not happen. A convincing Status header is not evidence.
3. **The leader owns every plan's block, never the workers.** Per-worker block edits do not compose past about two workers (`AGENTS.md`), and a map regenerated on a branch must never be committed. Workers report the new `**Landed:** / **Gate:** / **Next:**` values; the leader writes them to `<base>` as a Direct docs commit, and CI regenerates the map. Fence `docs/plans/**` out of every worker prompt.
4. Propose the batch in **one message**: candidates with your pick and priority, and every per-item business question front-loaded with your recommended answer (`ship-a-feature` style). No worker count — capacity decides that (Phase 2).
5. `go` = all your picks. Record everything in `decisions`. After this point you do not check in for anything outside the escalation contract.

## Phase 2 — admit, then dispatch

**Admit by measured capacity; never pick a worker count.** Before every dispatch run `pnpm agent:capacity`: exit `0` admits one more worker, `2` means not now — the item waits in `batch.json`'s `queue` — and `1` means a signal was unreadable, so read its output before overriding it. Re-run it whenever a worker enters CI-wait (it then holds no local capacity) and whenever one finishes. It reads three things, each a limit a batch has hit, with the limits from `orchestrate.config.json`:

- **A self-hosted CI queue's oldest job** — only when `capacity.ciQueue` declares one. Self-hosted lanes are a fixed budget: ten PRs from two batches once serialised for hours behind two heavy lanes that could never co-schedule, and every review round re-queues. A fleet larger than CI admits only lengthens the queue. Hosted runners do not queue on you; leave `ciQueue` out and the check says *not measured*.
- **Free RAM** (`capacity.minAvailableMb`) — every worker's typecheck, unit suites and landing checks run locally.
- **Local emulator suites** (`maxConcurrentEmulatorSuites`) — measured, never guessed: two concurrent suites on a 16 GB WSL2 host, not three.

**Usage quota is not measured, and is a batch constraint too** — a worker died mid-review-round on a session limit. Recovery is the same as for a crash (below).

**Dispatch with the script, never by hand:**

```bash
printf '%s' "<WORKER PROMPT>" > "$SCRATCH/<name>-prompt.txt"
pnpm agent:dispatch <name> <branch> "$SCRATCH/<name>-prompt.txt"   # --dry-run prints the plan
pnpm agent:dispatch --resume <name>                                 # relaunch `claude --continue` in its worktree
```

It cuts `<wt-dir>/<name>` off a fetched `origin/<base>` with its submodule, opens one `<project>-fleet` window per worker (panes vanish when a worker exits and take its last output with them) that stays open after exit, launches `claude -n <name> --permission-mode auto` with inbound peer messages accepted, answers the startup dialogs, and confirms the run started. A failure before launch rolls the worktree and branch back; exit `1` means the worker died during startup, and exit `3` means it launched but never showed it was working — read its window before re-dispatching. Every one of those steps once failed silently when hand-copied: six workers sat idle behind a settings dialog on 2026-09-08 until the leader noticed. If the script cannot do something, fix the script; do not fork the recipe back into this file.

The user watches with `tmux attach -t <project>-fleet` and may type into any window — treat anything they typed there as a user decision when the worker reports it.

**The permission classifier will not let you write a prompt that carries writes to a protected environment** (which envs are protected is the repo's `AGENTS.md`; a repo can declare its policy to the classifier — see the agent-skills README, *Auto-mode policy*). A worker prompt file containing `firebase functions:delete` and backfill `--apply` commands against beta and prod was blocked twice, with the rest of the prompt untouched. Do not split it across tools to get it through: `SendMessage` names that exact move as permission laundering and forbids asking a peer to do what your session was blocked on. The shape that works is to dispatch the worker with the *code* half only, surface the data ops to the user as a decision, and once the user gives an explicit go, **run them from the leader session yourself** (dry runs first, every command and count logged into `batch.json`) and message the worker the outcomes to fold into its PR. That is the one implementation-shaped thing a leader does, and only under an explicit go.

**Check file overlap before dispatching, and fence deletions as carefully as edits.** Two workers were handed the same doc deletion minutes apart. Name the files each worker owns and the files it must not touch, in its prompt.

### Worker prompt template

Every worker prompt MUST contain all of these blocks — fill `<>`.

**Carry the essential procedure inline. Never rely on a skill name alone**: both batches ran with `ship-a-feature` unresolved (a broken symlink), and the batch whose prompts inlined the procedure produced five correct PRs anyway while the other did not notice for hours. A referenced skill can silently fail to load; an inlined sentence cannot.

```
You are worker <name> in orchestrated batch <batchId>. Your task: implement
<plan path or ad-hoc description>. Scope decisions already made by the user —
do NOT re-ask them and do not open an interview: <decisions>.

Files you own: <paths>. Files another worker owns — do not touch, do not
delete: <paths>.

Procedure: you are already in your worktree on branch <branch>. Source
scripts/agent-env.sh and report your slot number. Implement, then open a PR
to <base> (`gh pr create --base <base>` <+ `--label <reviewLabel>` when the
land config names one>), then land it with `pnpm pr:land`, acting on its exit
codes: 0 merged · 10 CI red · 20 review requested changes (fix the cause,
push, rerun) · 30 stop and report · 40 preflight failed. Push once when the
branch is ready, not per commit. <The PR-body requirements the repo's
AGENTS.md sets, inlined — e.g. how a user-visible change is verified on a
device, or how its use is measured.>

Land with `scripts/pr-land-bg.sh`, from the start, not as a recovery step: it
runs `pnpm pr:land` in its own tmux session, which survives where a
backgrounded `setsid nohup … &` was reaped mid-poll again and again, and it
keeps the output to read afterwards. `--status` says whether yours is running,
`--attach` watches it, `--kill` stops yours alone. Do NOT touch the working
tree while it polls — that is how a run exits 40.

**Exit 143 is not an outcome.** It is SIGTERM: the harness reaped the run, and
the PR is in whatever state it was already in. Relaunch under `setsid` and read
the real state from GitHub. Treating 143 as a landing signal is how a batch
records a merge that never happened.

NEVER kill a lander with `pkill -f` / `pgrep -f` / `killall` — they self-match
the calling shell (the mystery exit 144), and a pattern-wide kill hits every
other worktree's lander on the machine; killing the pnpm wrapper by pid
orphans its `node scripts/pr-land.js` child, which keeps polling and can
merge a head missing your latest commit. `scripts/pr-land-bg.sh --kill`
stops only this worktree's, child before wrapper.

Never read an exit code through a pipe: `cmd | tail; echo EXIT=$?` reports
tail's status, and this has already produced a false "exit 0" for a run that
exited 30. Capture the code directly, then verify the real outcome against
GitHub: `gh pr view <n> --json state,mergedAt`.

Git restrictions — absolute, and they apply EVEN IN THIS THROWAWAY WORKTREE:
NEVER run git reset, commit --amend, checkout --/restore, clean, rebase -i,
add -A, add ., or any --no-verify / --force. Rebase onto origin/<base> only
when pr:land tells you to, and then with --force-with-lease. Reaping the
worktree is the leader's job, not yours — do not remove it. <Which deploys and
data operations are yours, per the repo's AGENTS.md — e.g. "Deploys and data
operations on dev and beta are yours to run when your work needs them — dry
run first, back up what a write touches, and report each with its counts.">
Never deploy to production and never write production data; read-only reads
of prod (an audit, a log query, a SELECT) are fine — a plan cannot be retired
without them. If you believe you need a forbidden operation, STOP and report
BLOCKED.

Reporting: after each milestone — slot claimed, plan settled, PR opened
(number), each review round, landed, or blocked — do BOTH:
  1. SendMessage to '<leaderSession>' with one terse line
     "<name>: <status> — <detail>";
  2. append the same line with a timestamp to
     ~/.claude/orchestrator/<project>-<leaderSession>/reports/<name>.log
A file survives a crash; a message does not. If SendMessage cannot find the
leader, do NOT guess another session — keep working and keep writing the log.
Escalate nothing yourself; route every question to the leader and continue
with whatever does not depend on the answer. <When a reviewer is wired:> the
review cap (maxReviewRounds) counts REVIEWER postings, not your pushes; when
the land config's roundsExhausted is "merge", pr:land merges on green at the
cap — report the capped round's findings to the leader before it does.
When you finish (landed or stopped), send a final report and exit.
```

**Plan finish line — add it whenever the task is a plan in `docs/plans/`.** Without it a worker stops at its first merged PR and the plan sits half-done with `Gate: none`:

```
Your finish line is the PLAN, not a PR. Loop: do the plan's Next → PR → land
→ re-read the plan against <base> → next step. Stop only when the plan is
fully done, or the next step needs something outside your reach (a release, a
soak, a beta/prod write, a deploy, a human, a decision). Several PRs are
expected. Do NOT edit anything under docs/plans/ — the leader owns every plan
file. Your final report is exactly one of:
  RETIRE <plan path> — <evidence it is done, incl. prod verification if it has a runtime surface>
  BLOCK <plan path>
    **Landed:** <none|dev|beta|prod|n/a>
    **Gate:** <release:x.y.z | soak:<trigger> | decision:<question> | blocked:<why>>
    **Next:** <one imperative line a cold worker can start>
    evidence: <PRs merged, what was verified>
```

## Phase 3 — supervise (event-driven)

You react to worker reports; you do not poll on a timer. Read both channels — SendMessage and `reports/*.log`. On each report: update `batch.json`, then decide:

- **Routine** (plan settled, PR open, review round below the cap, landed): just record it. The cap is the land config's `maxReviewRounds`, and `roundsExhausted` says whether the cap merges on green or hands over — re-read both on resume, the cap moved mid-batch once.
- **Worker asks a question you can answer from the plan or the recorded decisions**: answer it yourself. That is your whole purpose.
- **Escalate to the user** — only these:
  - anything on the repo's **hard-stop list** — the land config's `hardStop` patterns, its `Breaking-Client:` trailer, a PR targeting a protected branch, and any write or deploy `AGENTS.md` reserves for the user (always production). Nothing else: what the repo's `## Approval` frees is not an escalation;
  - a worker **blocked** or at the **review cap** — and read the capped round's finding yourself before applying "merge on green": on 2026-09-09 the third and final round on #914 found the rule depended on `request.query.where`, which the Firestore rules API does not expose, so the emulator-green PR would have denied live reads in prod. A substantive finding at the cap is a decision, not a formality;
  - a **product question** a worker raised that neither the plan nor `decisions` answers — what a user sees or can do, a rule, a migration, a cost (a technical choice is the worker's own, and you send it back);
  - a **genuine scope change** (the work invalidates a recorded decision or the plan it was dispatched on);
  - a suspected **cross-worker conflict**.

  Record it in `needsUser`, and move it to `decisions` once answered.

### Decisions go to the session the user talks to

The user talks to **one** session. When that session dispatched you (your launch prompt or a message from it says so), it is where your decision blocks go: `SendMessage` it the block instead of ending your own message with it — nobody reads your window. Name in the block any item you need confirmed **in your own window**: a peer message cannot carry the user's approval, so act on a relay only for reversible plan edits, never for a deploy, a data write or a hard-stop merge. The reverse is allowed: when the user gave an answer **to that session directly**, that session may apply the item itself and tell you, and you record it in `decisions` and do not act twice — the guard is about who the answer came from, not which session runs the command.

### Every stop with an open decision ends with the decision block

The user reads the **end of your last message**, not the scrollback. So whenever `needsUser` is non-empty when you stop, the message ends with this block — after the status, as the final thing on screen, and re-rendered at every stop until each item is answered. One entry per decision, each understandable by someone who has not read the batch:

```
## Needs your decision

1. <short name> — reply `<short yes reply>` or `<alternative>`
   What: <the action in plain words — which command, which env, what it changes, with dry-run counts>
   Why: <the concept: what this step is for, and what it unblocks>
   If yes: <what happens, the risk, how it is undone>
   If not now: <what stays blocked — and that nothing else waits on it>
   My pick: <recommendation, with the one reason that decides it>
```

Example entry:

```
1. Medals reconcile on beta, then prod — reply `go medals` or `hold medals`
   What: backfill-medals-showcase-reconcile.js --env=beta, then --env=production, backup first.
         Prod dry run: +7 medals, −13 medals, 52 counters repaired; 4 players drop to zero.
   Why: the finalize trigger is now the only medal writer; the 13 removals are medals the old
        client double-wrote. Until this runs, showcases disagree with the trigger's truth.
   If yes: showcases match the trigger; 2 silvers visibly vanish; restore from the backup.
   If not now: medal-single-writer stays blocked; every other worker continues.
   My pick: go — the 0.40.0 wall it was waiting for is live on both envs.
```

**Re-derive a hard-stop from the diff; never trust a worker's classification.** A worker reported "exit 30 hard-stop" on a PR whose files matched no hard-stop pattern — it had been starved of a CI runner. Check the PR's own file list against `.agents/land.config.json`'s `hardStop` patterns and its commits for a `Breaking-Client:` trailer before escalating, and check whether the stop was really a deadline: `pr:land` returns 30 for hard-stop, draft, closed, rounds exhausted, **or a deadline**.

**The user may delegate the hard-stop merge click to you** ("merge on green once the rounds are done, only tell me about decisions"). Record it in `decisions`, and then for each exit-30 PR: re-derive the hard stop from the file list and the trailers, read what triggered it yourself (a `Breaking-Client:` trailer means reading what it walls and why it could not be made non-breaking), confirm `gh pr checks` green and `reviewDecision`/last review on the *current* head, and only then `gh pr merge <n> --merge`. A follow-up commit pushed after the final round has no review bound to it — read that diff too before merging.

**"Stopped, tree clean" is a report, not evidence either — check the worktree before you merge.** A worker reported stopping twice and kept working through a fifth review round; the leader merged on that report and found the uncommitted work only when reaping. Run `git -C <worktree> status --porcelain` **before** merging a worker's PR. Expect to see a mutation in flight and do not panic at it: verifying that a new test can actually fail means the tree transiently holds a deliberately broken source file, restored moments later — so read the diff before concluding anything, and never merge or commit from a worktree mid-verification.

**Never let "done" stand without evidence.** A merge is `gh pr view <n> --json state,mergedAt` saying `MERGED`, never a worker's word and never an exit code read through a pipe.

**A red reviewer job is not a review.** Twice in one batch every worker's PR went red on the reviewer job itself — provider chain down, quota exhausted — and the workers correctly read the log instead of pushing code. Check that job's log before counting a PR as blocked, and expect the worker sessions to be paused on the same usage limit as the reviewer's Claude accounts, since it is one account.

**Silence**: check the worker's window (`tmux capture-pane -pt <project>-fleet:<name>`) and `ListAgents` before assuming failure. A worker that exited leaves a live window with its last output; a worker that is alive but stuck is a different problem. If it died (crash, quota, a machine restart that took the tmux server), `pnpm agent:dispatch --resume <name>` — the worktree is warm and its transcript carries its context. A worker that was mid-`pr:land` lost its lander too: tell it to relaunch one.

**A worker lands**: mark `landed`, reap it with `scripts/agent-reap.sh <name>` (Phase 4), then `pnpm agent:capacity` and dispatch the next queued item if it admits.

**A cause is proven from the failing attempt's own evidence, never from what the build contains.** On 2026-11-20 a worker blamed two e2e failures on App Check because that build still had it; asked to show the signature (the transport error, the backend timeout, the banner) on those exact attempts, it found none and withdrew it. When a worker attributes a failure, ask for the log lines or the trace from that attempt; an attribution without them is a hypothesis, and the leader records it as one.

**Intermittent failures: get the hit rate before spending runs.** Give every diagnostic worker an explicit budget of CI runs (and branch prefix, deleted when done) in its prompt. Before a run meant to catch an intermittent failure, have the worker count the per-attempt hit rate across every run so far; at 30% a single run comes back green 70% of the time and proves nothing, while one dispatch repeating the flow on every shard can make a hit likely. The same rate decides what a soak can show: when the bug appeared in 1 of 4 runs before the fix, three clean runs after it would also happen with no fix about 42% of the time. Retire such a fix on a test that reproduces the bug, and let the runs show only that nothing regressed.

**A worker that finds a bug outside its files reports it; the leader routes it.** Diagnostic work surfaces neighbours: a crash, a write loop, a stale test. Don't widen the worker's scope. Verify the finding is approved work (a crash is a defect fix), file it as a plan, check nobody else is editing those files (below), and dispatch a separate worker. Its plan's gate can then name the first worker's runs as its soak.

**Don't name who owns a commit on `<base>` from its author line.** Every session commits as the same human. When `<base>` goes red under the batch, quote the commit and the failing check to the sessions that might own it, and ask; on 2026-11-20 the leader named the wrong session. Never fold someone else's fix into a worker's PR without that owner's word.

Never relay a worker's message as if it were a user decision.

## Phase 4 — wind down

When all workers are terminal:

1. Final `batch.json` update (statuses, PR numbers, merge evidence).
2. **Reap**, per worker, with `scripts/agent-reap.sh <name>` (`--dry-run` to preview). It does exactly the following, and refuses with exit `2` when either check fails. Never type these steps by hand: the leader of the 2026-11-20 batch did, and reached for `--force` every time. `git -C <worktree> status --porcelain` is empty and `git merge-base --is-ancestor <worktree HEAD> origin/<base>` holds — then free its slot (`(cd <worktree> && source scripts/agent-env.sh --clean)`), `rm -rf` the directory, `git worktree prune`, and `git branch -d <branch>` (it refuses an unmerged branch; let it). Never `git worktree remove --force`: git refuses a plain `remove` on any worktree in a repo with a submodule, which is exactly what tempts `--force` past a tree the two checks above would have caught. A worktree that fails either check is reported, not reaped. This is the leader's job precisely because workers are banned from it, and the ban stays absolute, since a carve-out is indistinguishable in a prompt from the destructive uses it exists to prevent.
3. **Write the learnings down** where a future agent will find them — `docs/plans/` or `docs/{decisions,ops}/`, not `batch.json`, not the chat.
4. One summary to the user: landed PRs, anything aborted or failed and why, leftovers that belong back in `docs/plans/`.

Leave `batch.json` in place — it is the recovery record until the next batch.

## Anti-patterns

- **Implementing anything yourself.** Even "just this one small fix" — dispatch it or queue it.
- **Coordinating from memory.** If it's not in `batch.json`, it didn't happen.
- **Treating `batch.json` as a durable record of repo debt.** It is coordination state; findings go in `docs/`.
- **Escalating for reassurance.** "Worker opened a PR, ok?" is not an escalation category.
- **Swallowing an escalation category** to keep the user undisturbed.
- **Polling workers on a timer** or streaming their transcripts into your context — you need milestones, not logs.
- **Dispatching two workers into overlapping files** — or handing the same deletion to two workers.
- **Merging on a worker's word that it has stopped.** Check its worktree is clean first; a worker can be three rounds past its last report.
- **Picking a worker count** — any number, the user's or yours. `pnpm agent:capacity` admits one at a time.
- **Hand-rolling the dispatch recipe** — `pnpm agent:dispatch`; fix the script when it falls short.
- **Hand-rolling a reap** — `scripts/agent-reap.sh`; `git worktree remove --force` skips the very checks a reap needs.
- **Accepting an attribution without the failing attempt's evidence** — "the build had X" is not "X caused it".

## A note on running two leaders

The user may run a second orchestrator. It works, with two conditions: separate state directories, and a split announced between leaders before dispatch — by pool or by named plan list, plus the files each side owns. Two `advance-plans` leaders over the same pools without a split will dispatch the same plan twice. Shared skills are edited by exactly one leader, after both batches are terminal.
