---
name: managing-plans-lifecycle
description: Use when creating a plan or design doc, promoting one between stages (ideas → ready → ongoing → retired), starting or resuming in-progress work, retiring a finished plan, or surveying what work is in flight. Defines the `docs/plans/{ideas,ready,ongoing}/` convention, in which one file per topic moves between folders as the work matures, deleted once the code is the source of truth.
---

# Managing the plans lifecycle

Plans are temporary coordination docs. They exist to make upcoming or in-progress work findable; once code, tests, release notes, and operational state are the source of truth, the plan is deleted. Each stage has its own folder under `docs/plans/`; the file moves between folders as the work matures. **Code is the source of truth — finished plans are not kept.**

> **Layering.** This skill defines the *lifecycle* only. Everything repo-specific — which stages the repo uses, whether the priority label is required, what counts as "verified", where cross-repo plans live — belongs in the repo's own agent instructions (`AGENTS.md`, `CLAUDE.md`, or equivalent), **which win over this file wherever they differ.** Referred to below as *the repo's instructions*.

This skill owns the *lifecycle* — where plan files live, when they move, and what the `ongoing` status header contains. It does **not** own the *content*: the design questions, the task breakdown, the engineering judgement. Whatever you already use to think a plan through keeps that job.

## Folder layout

```
docs/
├── plans/
│   ├── ideas/        # Proposals. May or may not happen. No tasks required.
│   ├── ready/        # Decided to implement. Plan/tasks written. Not started.
│   └── ongoing/      # Being implemented. Status header at top is required.
│       └── soak/     # OPTIONAL stage — implementation done and verified in
│                     # production; only elapsed-time soak remains. See below.
├── decisions/        # Durable rationale, written when a plan retires.
├── incidents/        # OPTIONAL — production incident and notable-bug records.
└── ops/              # OPTIONAL — operational recipes, credential/config facts.
```

One file per topic. **Same filename throughout the lifecycle** — only the directory changes.

`soak/`, `incidents/` and `ops/` exist only in repos whose instructions declare them. A repo that ships continuously and has no store-release or backfill contract does not need `soak/`; don't create it speculatively.

## Priority label (per-repo)

Some repos require every file under `docs/plans/{ideas,ready,ongoing}/` to declare exactly one priority label: `low`, `medium`, or `high`. **Check the repo's instructions before enforcing it** — where the convention is not declared, do not add the field to files that lack it, and do not flag its absence as a defect.

Where it *is* required:

Use this metadata line near the top of `ideas/` and `ready/` plans, immediately after the title unless the file already has a compact metadata block:

```markdown
**Priority:** medium
```

For `ongoing/` plans, include the same label in the required Status section:

```markdown
- **Priority:** medium
```

Priority means:
- **high:** security, data integrity, release blocker, live-user breakage, or work that materially reduces operational risk.
- **medium:** decided product/platform work with clear value, important maintainability, or a known pain point that is not urgent.
- **low:** speculative, parked, future-facing, cleanup-only, or work that should wait for a stronger trigger.

When creating or promoting a plan, set the priority deliberately. When surveying plans, call out missing or stale priorities before discussing order. Do not leave `TBD` or omit the field.

### File naming — date events, never date living documents

Names are bare kebab-case: `app-check-rollout.md`, `image-cropper-ui.md`, `deploy-integrity-guards.md`.

The rule across every folder:

| Folder | Prefix | Because |
|---|---|---|
| `plans/{ideas,ready,ongoing}/` | **no date** | the file moves through stages under one name |
| `decisions/` | **no date** | a decision is revisited and amended, not superseded by date |
| `ops/` | **no date** | a recipe is edited in place; a date makes a current doc look stale |
| `incidents/` | **`<date>-`** | an incident happened on a day — the date sorts it and never rots |

**Date what is an event. Do not date what is alive.** A dated filename on a living document
forces every future reader to open it just to find out whether it still applies.

**No date prefix anywhere under `docs/plans/`** — not in `ideas/`, not in `ready/`, not in
`ongoing/`. Many planning tools default to `YYYY-MM-DD-<topic>-design.md`; when one does,
**strip the date prefix as the file lands in `docs/plans/ideas/`**.

Why plans in particular must not carry one:
- The filename is stable across the lifecycle (idea → ready → ongoing). A date that was meaningful when the proposal was drafted becomes misleading by the time implementation starts.
- Git log + the `ongoing` Status header carry every timing question worth answering: when it was proposed, when it was promoted, when it was last touched.
- The folder is the meaningful coordinate, not the date.

If a file lands in `docs/plans/` with a date prefix, rename it on the spot — don't leave it for later. Same applies to file imports from other repos: drop the date during the move.

### Which repo a plan lives in

This skill describes *how* plans move through the lifecycle. It does **not** decide *which* repo a given plan belongs in — that is per-repo policy. As a general principle: single-repo plans live with the code they describe; cross-repo plans live in whichever repo is designated canonical for cross-cutting work. When in doubt, check the repo's instructions before creating a plan.

### Folders this convention does not use

Not `docs/archive/`, not `docs/plans/{queued,blocked}/`, and no tool-specific scratch folder:
- **No tool-specific plan folder.** Whatever your planning tool calls its own output directory, the file belongs in `docs/plans/ideas/`. One index, not two.
- **No `docs/archive/`.** Shipped plans are deleted, not archived — an archived plan is a stale snapshot that lies to a future reader as soon as the code drifts. Git history (`git log -- docs/plans/<slug>.md`, `git show <sha>`) recovers any deleted plan.
- **No `queued/` or `blocked/`.** "Decided, ready to start" is `ready/`. "Waiting on a trigger/decision" is `ideas/` if undecided, or `ready/` with the gate stated inline if decided. "Maybe never" is `ideas/`.

## When to invoke this skill

- About to brainstorm or plan something → use it to know where the output should land
- About to promote a plan between stages → use it for the file move + content edits
- Starting or resuming work on an `ongoing/` plan → read the status header **first**
- A plan's implementation is merged → use it to distill into `docs/decisions/` and delete the plan
- The user asks "what plans are in flight" or "what's the status of X" → start from `docs/plans/ongoing/`

## Transitions

### Creating a new plan → `ideas/`

A new plan lands in `docs/plans/ideas/<topic>.md` directly. No separate spec/plan file split — one file evolves through the stages. If your planning tool writes it somewhere else, or with a date prefix, move and rename it on the spot.

A new `ideas/` doc should contain at minimum:
- **Priority:** `low`, `medium`, or `high`
- **Goal:** one sentence
- **Context:** why this is being proposed
- **Design / approach:** the actual proposal
- **Open questions:** what's still undecided

No checkboxes required at this stage. The file might sit here for months or get deleted unimplemented — both are fine.

### `ideas/` → `ready/`

The decision has been made to implement. Before moving:

1. Resolve or accept the open questions inline (delete the section once empty, or rename to "Out of scope" with the rejections).
2. Set or re-check the mandatory **Priority** label.
3. Add a **File Structure** section listing files to create/modify/delete.
4. Add **Tasks** with `- [ ]` checkboxes, grouped into stages. Use whatever planning tool you prefer for the breakdown if the plan is non-trivial.

Then `git mv docs/plans/ideas/<topic>.md docs/plans/ready/<topic>.md`.

### `ready/` → `ongoing/`

Implementation is starting. Before moving:

1. Insert the **Status** section as the first `##` in the file, above any existing content (after the title and Goal line).
2. Fill in the initial values.

Then `git mv docs/plans/ready/<topic>.md docs/plans/ongoing/<topic>.md`.

#### The Status section (required in `ongoing/`)

```markdown
## Status

- **Updated:** YYYY-MM-DD
- **Priority:** low | medium | high
- **Stage:** which task/section is currently in progress
- **Branch:** repo `branch-name` (or "n/a — multi-repo" with a list)
- **Done:** what's verifiably complete (one bullet per chunk, terse)
- **Next:** the immediate next action
- **Blockers:** any open questions or external dependencies
- **Handoff:** non-obvious context another agent needs to resume — env state, regen steps, "rerun X before pushing", anything not visible from the diff
```

Update the Status section:
- At the **start** of every work session (set `Updated`, refresh `Next` and `Blockers`)
- At the **end** of every work session (move items from `Next` to `Done`, refresh `Handoff`)
- Whenever a blocker resolves or a new one appears

The Status section is the contract with the next agent (or future you). If a field doesn't apply, write `none` — don't omit it.

#### Rollout / phase table (keep it when the plan has one)

Some repos ship code on dev → beta → prod at different times and run per-env backfills. A prose Status section alone can't tell "Phase 1 shipped" from "Phase 1 shipped on dev only, beta pending." When an `ongoing/` plan has env-specific or multi-phase state, **keep a verifiable progress table below the Status section** (an env-rollout matrix or a phase table). The Status section is the human summary; the table is the verifiable state — a plan that lacks one can stall silently.

```markdown
## Rollout status

| Step | Dev | Beta | Prod |
|---|---|---|---|
| Code deployed | ✅ | ✅ | ⬜ |
| Backfill executed | ✅ | ⬜ | ⬜ |

Legend: ⬜ pending · ⏳ in progress · ✅ done · ⚠️ blocked (note inline)
```

### `ongoing/` → `ongoing/soak/` (optional stage)

Only in repos that declare `soak/`. Move a plan there when **all** of these hold:

- The implementation phase is complete.
- Required deploys, markers, or backfills are **verified from source-of-truth evidence**, not from a checklist.
- No normal implementation task remains.
- The only remaining work is letting the deployed state soak before a later contract step — a cleanup, a strict-read flip, a legacy-field removal, or a hard/minimum-supported-version release.

Keep it in plain `ongoing/` when production has not been verified yet, implementation still needs code, a required backfill has not run on every env, or the next step is engineering work rather than elapsed time.

When moving into `soak/`: record exact evidence in the Status block (env, marker path or release, date, counts where available, and the remaining trigger); update relative links in the moved file and inbound links from related plans and scripts; and leave the next action concrete — *"after soak, set backfill gates to vX.Y.Z and remove the legacy fields"*, never *"follow up later"*.

### `ongoing/` (or `soak/`) → retired (plan deleted)

**"Merged" is not the gate — "verified" is.** Never retire a plan because the code was written or the PR landed; retire it when the behaviour is confirmed in the environment that matters. Where a repo ships through staged environments, that means the *final* one, not the first.

1. Open the plan and identify what durable rationale is worth keeping. Use this rubric:
   - **Keep** (move to `docs/decisions/<topic>.md`): non-obvious design choices, rejected alternatives with reasons, invariants the code enforces but doesn't explain, dependencies on external systems / contracts.
   - **Keep** (move to `docs/incidents/<date>-<slug>.md`, where the repo has that folder): production incidents and notable bugs — what happened, scope, recovery. Link out to the `decisions/` doc that fixed it rather than restating it.
   - **Keep** (move to `docs/ops/<slug>.md`, where the repo has that folder): operational recipes and credential/config facts that aren't a decision.
   - **Delete**: task lists, file-by-file checklists, "how we did it" prose, status headers, rollout tables, anything visible by reading the code or `git log`.
   - **Delete**: outdated assumptions, open questions that got answered by reality.

   **Default to deleting outright.** Most shipped plans warrant no durable doc at all. Do not write a decision doc to summarise a completed plan, and never when the *why* is already recoverable from git, a closed issue, an upstream source, or an existing decision. A decision doc nobody needs is debt.

2. If anything was kept, write it using an ADR-lite shape — **Context / Decision / Rejected alternative / What this binds / Revisit-when** (match existing files in the target folder). Keep it short — one decision per file, focused on *why* not *what*. Operational step-by-step procedures belong in a skill or `ops/`, not in `decisions/`.

3. **Delete** the plan file: `git rm docs/plans/ongoing/<topic>.md`. Do not move it to a `done/` folder. Do not keep it "for reference." Code is the reference.

Commit message: `docs: retire <topic> plan; extract decision` (or just `docs: retire <topic> plan` if no decision was extracted).

## Surveying in-flight work

When asked "what's in flight" or "what plans do we have":

1. `ls docs/plans/ongoing/` — what's actively being worked on. Read each file's Status section.
2. `ls docs/plans/ongoing/soak/` — done, waiting on elapsed time (where the repo uses it).
3. `ls docs/plans/ready/` — what's queued.
4. `ls docs/plans/ideas/` — what's been proposed.
5. `ls docs/decisions/` — what's already been decided and shipped (durable record).

Don't grep for completion via checkboxes. Folder location is authoritative.

When *auditing* rather than listing, the bar is higher: for each plan that looks stale or misplaced, **state the evidence from the code, not from the filename** — the service exists, the flag is flipped, the backfill marker is present — then recommend exactly one action: keep, promote, move to ongoing, soak, retire, or extract-then-retire. A recommendation resting only on a plan's own prose is worthless; the plan is the thing under suspicion.

## Anti-patterns

- **Adding a `done/`, `completed/`, `archive/`, `queued/`, or `blocked/` folder.** Done plans are deleted, not archived. The history is in git + decisions. Decided-not-started is `ready/`; gated/speculative is `ideas/`.
- **Keeping the date prefix.** Dates rot as the plan evolves; the filename should be stable across the lifecycle.
- **Skipping the Status header on `ongoing/`.** A plan without a Status header is unusable for handoff — fix it before doing any other work.
- **Moving a plan into `ongoing/` without a rollout/phase table when it has per-env or multi-phase state.** The Status line alone hides which envs shipped.
- **Writing a decision doc that restates the implementation.** If a future reader could learn it by reading the code, it doesn't belong in `docs/decisions/`.
- **Promoting `ideas/` → `ready/` without resolving open questions.** Move the questions to "Out of scope" or answer them. `ready/` means decided.
- **Marking work `ongoing/` before implementation actually starts.** `ready/` is where decided-but-unstarted work waits; an `ongoing/` plan nobody is touching makes every real one harder to find.
- **Retiring a plan because the code merged.** Merged is not verified. Retire on confirmed behaviour in the environment that matters.
- **Inventing a parallel lifecycle taxonomy.** If a state feels unrepresentable, it is almost always `ideas/` (undecided) or `ready/` with the gate stated inline. Adding a folder fragments the index for everyone.
- **Burying unresolved follow-up work in a PR or chat message.** Those vanish. Update the plan, or create one.
- **Enforcing a convention the repo never adopted** — the priority label being the usual case. Check the repo's instructions first.
