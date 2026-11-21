---
name: review-ideas
description: Use when the user wants docs/plans/ideas/ checked against the code — "review the ideas", "audit the backlog", "which ideas are outdated / shipped / duplicated", "clean up ideas/" — or when the approved pool is running dry and the user should be asked for yeses on verified ideas.
---

# Review ideas — check the backlog against the code

`ideas/` is the unswept inbox, and its plans lie: on 2026-10-31, 48 of the 60 least recently reviewed ideas (80%) carried a false claim or a shipped part, and 9 of them were deleted. This skill is the recurring check that keeps that rate down. It is a **docs-only** batch: read-only scouts verify, one leader runs git, nothing is implemented, and **nothing reaches `ready/` without the user's yes** — `ready/` means *approved* (the repo's `AGENTS.md` `## Approval`; without that section nothing is pre-approved and every PROMOTE is NEEDS-YES). Its product is those yeses, asked on facts it has just verified. An idea the standing policy already pre-approves needs no yes and no move: the review tags it (§4) and `advance-plans` builds it straight from `ideas/`.

**It is not an `orchestrate` batch.** No PRs, no CI lanes, no tmux: scouts are in-session background subagents (Agent tool), about 15 ideas each, at most 8 at once — the ceiling is usage quota, which the reviewer and every other session share. Repo facts (`<project>`, `<base>`, `<wt-dir>`) are `orchestrate`'s. It may run beside an implementation batch: it edits `ideas/` bodies, and touches anything else only to fix a backlink, under §4's rules.

## 1. Snapshot and select — rolling, oldest-reviewed first

1. Work in a docs worktree at `origin/<base>`, never the main checkout (it is the user's, and usually dirty): `git fetch origin <base> && git worktree add --detach <wt-dir>/ideas-review origin/<base> && git -C <wt-dir>/ideas-review submodule update --init`. Dispatched into a worktree already (`pnpm agent:dispatch` cuts one off `origin/<base>`)? Use that one — its branch is never pushed, since you push `HEAD:<base>`. Run every command below **from inside it**: the scripts read the history of the checkout they sit in.
2. `node scripts/ideas-review-order.js --limit 60` — ideas by last review, oldest first; a review is the later of the last real edit and the last `Reviewed-Idea: <slug>` trailer (§4). Review the whole folder only when the user says so ("all of them", "a full sweep").
3. Record the snapshot SHA, the list and the shards in `~/.claude/orchestrator/<project>-<your session name>/batch.json` (`ListAgents` prints your name first). Ideas added after the snapshot are out of scope.
4. **Fence** — verified, never edited, moved or deleted by this batch; findings go in the report, and **no trailer**, so they come up first next run:
   - changed by an open PR: `gh pr list --state open --limit 200 --json number,files` (not `--search`, which matches PR text, not files; without `--limit` it stops at 30);
   - changed on a live branch: for each `origin/*` ref with a commit in the last 7 days, `git diff --name-only origin/<base>...<ref> -- docs/plans/ideas` (not `git log --all`, which flags branches already merged);
   - committed on `origin/<base>` in the last 24 hours — another leader or the user just placed it. `batch.json` files carry no list of touched plans, so git is the only reliable fence.
5. **Pinned** — named by a file this batch cannot edit: anything non-Markdown (`git grep -lw '<slug>' -- ':!*.md'` — a whole-word match, so a prose mention counts and a substring does not), the vendored `managing-plans-lifecycle`, or the `.agents/_shared` submodule. A docs commit cannot delete a pinned idea — it would strand the pointer — so a pinned SHIPPED or OBSOLETE idea is **retired by the leader through one code PR per batch** (§4), not asked about. One pin that PR cannot clear: a link from the vendored skill or the submodule cannot be edited from this repo, so that idea is rewritten in place to say what shipped and names the upstream fix it waits on.

## 2. Shard by code domain

~15 ideas per scout, grouped by the code they talk about (CI/landing, backfills, backend functions, app UI, analytics…) so each scout loads one area once. Put ideas that link to each other or share keywords in the **same** shard. Each idea belongs to exactly one scout; record the lists. Hand every scout the open PR list and live branch list from §1.4 — IN-FLIGHT detection needs them, since a branch can implement an idea without editing it.

## 3. Scout prompt — inline all of this, never just the skill name

```
You are scout <name> in ideas review <batchId>. Review exactly these files in
<worktree>: <list>. Fenced (verdict only, never edit): <list>. Pinned (never
delete): <list>. Open PRs / live branches: <list>. For each file: list every
load-bearing claim (a path, a symbol, "X is not done", a count, a PR number),
check each against the code in this worktree (git grep, git log -S/-G, git
show, gh pr view), then give ONE verdict below — plus DECISION alongside it
when the idea also hangs on a product question (e.g. REWRITE + DECISION: fix
the false claims AND state the question):
  VALID            every load-bearing claim holds → change nothing
  REWRITE          the core holds, but a claim is false, part shipped, or a
                   name moved → fix only what is false, in place; drop
                   shipped parts; add no scope; keep **Priority:** valid
  SHIPPED          implementing code + merged SHA/PR, AND the plan's own
                   retirement condition is met if it states one, AND for
                   runtime work `git merge-base --is-ancestor <sha>
                   origin/main` with its release live on prod — unsure → next
  SHIPPED-UNVERIFIED merged, but not live on prod or its own retirement
                   condition is unmet → also REWRITE it in place to say what
                   merged (SHA) and what is left to verify
  OBSOLETE         code or a docs/decisions/ doc proves the premise false
  DUPLICATE <file> same change as another idea; name which should survive
                   (the more current and broader) and why
  IN-FLIGHT        an open PR or live branch implements it
  PROMOTE          premise verified, and one yes/no from the user settles
                   it (no open design work). Any priority, any executor —
                   Visual or human-only work is approvable too. Name its
                   class by AGENTS.md ## Approval — PRE-APPROVED or
                   NEEDS-YES, quoting the line that decided it; a mixed
                   idea gets a class per part; unsure → NEEDS-YES. A yes
                   already written in the plan ("Decided <date> (user)")
                   counts: report it as approved
  DECISION         hangs on a product/business question → one-line question
                   + your recommended answer. Ask at the level of the GOAL
                   first: does the rule the plan protects still make sense,
                   and how would you design it from scratch? Offer that as an
                   option when it beats the plan's own alternatives — a
                   choice between two ways to patch a mechanism can hide that
                   the mechanism should go
Cite evidence as path + symbol (or SHA/PR), not bare line numbers. The plan's
own prose is never evidence ("shipped in #123" must be checked). Live data
needed → DECISION, naming the read-only query that would settle it.
You edit ONLY your own non-fenced files, only for REWRITE/SHIPPED-UNVERIFIED (and to
record a pinned SHIPPED idea as shipped).
NO git writes of any kind — no add/commit/rm/mv/stash/reset/restore/checkout
--/clean/rebase, EVEN IN THIS THROWAWAY WORKTREE. No tests, builds, installs or
emulators. Never touch ready/, ongoing/, the map, or another scout's files.
Need a forbidden step? Report BLOCKED.
Report one line per file: <slug> | <verdict> | <evidence> | <edit made>.
```

## 4. Leader — verify, write, commit

- **Re-run the evidence yourself** for every SHIPPED and OBSOLETE before `git rm`; read every PROMOTE in full; read every REWRITE diff and reject one that drops still-true content or adds scope. Resolve DUPLICATEs yourself, across shards too: merge into the survivor, delete the other.
- **Another human's ideas** — the adding commit's author, `git log --follow --diff-filter=A --format=%an -- <file> | tail -1`, is a human who is not the user (a co-founder, a collaborator; `Claude` authors are the user's agents) — are deleted only on SHIPPED. OBSOLETE becomes a DECISION, since the idea is theirs to drop.
- **Deleting fixes every Markdown backlink in the same commit** (`git grep -l '<slug>\.md' -- '*.md'` — not `ideas/<slug>`, which misses a sibling idea's relative link), except: a backlink inside `ongoing/` or `ready/` → do not delete; rewrite the idea to say it shipped and list it in the report, because that edit is another leader's file and would falsely reset its plan's `Advanced`. Links from history (`CHANGELOG.md`, `docs/incidents/`) stay as they are.
- **Pinned retirements: one code PR for the batch.** In a worktree off `origin/<base>` (`git worktree add <path> -b docs/retire-pinned-ideas-<batchId> origin/<base>`), for every pinned SHIPPED/OBSOLETE idea: repoint the pointer at what replaced the plan (the code, a `docs/decisions/` doc, a SHA) or drop the sentence if it only cited the plan, and `git rm` the idea with its Markdown backlinks — the same evidence bar and the same co-founder rule as any deletion. Land it with `pnpm pr:land` (labelled with the land config's `reviewLabel`, if any); it is code, so it goes through CI and the reviewer. Leave those ideas out of the shard commits, so the PR is the one place they are deleted.
- **A stale doc outside `ideas/`** that a scout's evidence contradicts (the services map, an ops doc, a skill): fix it in its own Direct commit when it is Markdown, and say so in the report; a stale comment in code goes in the pinned-retirement PR if one is open, otherwise in the report.
- **Live data:** with the user around, run the read-only query yourself — a read is never the user's to run. Unattended, leave it a DECISION.
- **One commit per shard**, `docs(plans): review ideas — <shard>: N rewritten, M deleted`, body listing each deletion with its evidence, and in the final paragraph next to `Co-Authored-By` a **`Reviewed-Idea: <slug>` trailer for every reviewed idea in the shard — VALID ones included** — except a fenced idea or one with an open DECISION: those get their trailer only in the commit that applies the user's answer, so an unanswered question is not buried at the back of the queue. Beside it, a **`Pre-Approved: <slug>` trailer for every PROMOTE PRE-APPROVED idea** (the whole idea, not one part of a mixed one) — that trailer is how `advance-plans` finds the ideas it may build without a yes, so write it only after you have read the idea in full and agree with the class. A shard with nothing to edit is a `git commit --allow-empty`. The trailer is the only trace a VALID verdict leaves, and a multi-file review commit is a sweep the plans map walks past; without it the next run re-reads the same ideas from the top.
- **Push in at most two waves.** Each wave: `node scripts/plans-map.js --validate; V=$?` — stop on `V≠0`; `git fetch origin <base>`; drop your edit to any idea `git diff --name-only <snapshot>..origin/<base> -- docs/plans/ideas` shows changed upstream (fence it); `git rebase origin/<base>`; validate again; `git push origin HEAD:<base>`. Never commit a regenerated map. Each push costs one map-workflow run.

## 5. One message to the user

Push the mechanical results first — they need no answer. Record each verdict in `batch.json` **with its one-line reason**, never the bare word: the reason is what the report is built from, and what a later run reads. **Dispatched by another session that is the user's interface** (the prompt or a message from it says so)? The message goes to that session with `SendMessage`, not to your own window, which nobody is reading; act on its relays for reversible plan edits, and name in the block any item you need the user to confirm in your own window. Then **one** message: counts per verdict, the staleness rate (non-VALID ÷ reviewed), each deletion with its SHA, held deletions and fenced findings, and the link to the **decision page** (below). The page carries `orchestrate`'s **Needs your decision** block, one entry each, with your pick:

- **PROMOTE, NEEDS-YES** → on yes, the leader writes the user's answers into the plan and `git mv`s it to `ready/`, one commit per plan with its trailer. **No File Structure, no Tasks** — the agent that starts the work writes those against that day's code (approve early, plan late — `managing-plans-lifecycle`). List them ranked by priority. A PRE-APPROVED one is not in this block and is not moved: it stays in `ideas/` with its `Pre-Approved:` trailer, and the report lists it under **buildable now** — `advance-plans` takes it from there, and its worker re-checks the class before building;
- **SHIPPED-UNVERIFIED / IN-FLIGHT** → move to `ongoing/` with a `release:`/`soak:`/`blocked:` gate — after any live `advance-plans` leader has wound down;
- **DECISION**, and another human's OBSOLETE ideas.

**The decisions go on a web page, with pictures, not in a long chat message** (decided 2026-10-31, user: text alone was not enough to decide on). Each decision is a self-contained card: the question in one line, then what it looks like, so the user judges the thing and not a description of it:
- a **user-visible change** → a before/after mockup of the screen (an HTML/SVG sketch of the real layout and copy — what the user sees today, what they would see), one per option;
- a **cost** → the numbers: the unit price with its source, the measured volume, the monthly total at today's volume and at 10×;
- a **mechanism or flow** → a small diagram of who writes what, today and after;
- a **term the plan uses** ("a short round", "a derived rail") → a concrete example drawn from real data or a fixture.

Then the options side by side, your pick marked with the one reason that decides it, and the reply token. Publish it with the session's artifact publisher when there is one — and **republish the same page** at each re-render rather than starting a new one; otherwise write one self-contained HTML file to the scratchpad and give its absolute path. The chat message stays short: the summary above, the link, and as its last lines the numbered reply tokens, since the user reads the end of the message.

Report the pinned-retirement PR by number and outcome — it is done, not asked.

**Every reviewed idea not promoted gets one line saying why** — an open DECISION, not a unit of work (a watch list, a log), needs new design before a yes could settle it — so the user can tell the bar from an oversight. Priority is never the reason: it orders the queue, it does not gate it. **When an answer closes a DECISION, test the idea against the PROMOTE bar again** and offer it in the same reply if it now passes: the question that held it back is gone.

**When the parent session holds the user's answer directly** (the user said it there, not through a relay), the parent may apply that item itself and tell the leader, which then records it and does not act twice — the guard against acting on relays is about who the answer came from, not about which session runs `git rm`.

`go` = all your picks. If the user delegated promotion up front ("move the good ones to ready"), apply it and still list every promotion.

## 6. Done

Every snapshot idea has a verdict in `batch.json`; every one without a fence or an open DECISION has a trailer on `origin/<base>` (from the docs worktree after the push, `node scripts/ideas-review-order.js` sorts them last); decisions applied or re-rendered; the measured staleness rate written into this skill's opening paragraph, replacing the older figure; the worktree reaped (clean, `HEAD` an ancestor of `origin/<base>`, `rm -rf`, `git worktree prune`).

## Common mistakes

| Mistake | Instead |
|---|---|
| One sweep commit, no trailers | One commit per shard, a `Reviewed-Idea:` trailer for every idea reviewed (not fenced, no open DECISION) |
| Reviewing the whole folder every time | `--limit 60`; a full sweep only when the user asks |
| Moving "clearly good" ideas to `ready/` yourself | Only what the user approved or delegated; a PRE-APPROVED idea stays in `ideas/` with a `Pre-Approved:` trailer |
| Holding back a `low` idea from PROMOTE | Priority ranks the proposals; it never excludes one |
| Writing File Structure and Tasks at promotion | The executor writes them when it starts — they rot in `ready/` |
| Deleting on "Shipped in #123" in the plan | Code + merged SHA + live on prod + the plan's own retirement condition |
| Scouts running `git rm` / `git mv` | Scouts edit their files; the leader alone runs git |
| Deleting a pinned idea in a docs commit | It goes in the batch's one pinned-retirement PR, with its pointer fixed |
| Asking the user before retiring a pinned SHIPPED idea | Not a decision — open the PR |
| Deleting an idea backlinked from `ongoing/`/`ready/` while their leader is live | Rewrite it as shipped and report it |
| Fencing with `gh pr list --search` or `git log --all` | `--json files`; `git diff origin/<base>...<ref>` |
| Working in the main checkout | Detached worktree at `origin/<base>` |
| The decision block as a long chat message | A web page, one visual card per decision; the chat carries the link and the reply tokens |
