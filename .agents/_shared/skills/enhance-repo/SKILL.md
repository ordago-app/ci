---
name: enhance-repo
description: Autonomous enhancement pass over one repo that files plans, never code. Measures the repo with `enhance-measure.js`, triages the evidence into coherent units, investigates each unit (in parallel on a large repo), then selects, classifies, fact-checks and files up to N `docs/plans/ideas/` plans under the repo's own rules — pre-approved only where its `## Approval` frees them. Ends with one report. Use when the user says "enhance this repo", "find improvements", "architecture pass", "find refactor opportunities", "what should this repo do next", or when the approved pool is running dry.
---

# Enhance one repo

A pass that turns evidence into plans. It **writes plans, never code**, and runs start to
finish without asking; the user's only touchpoint is the final report and a yes on the
plans that need one. Everything lands in `docs/plans/ideas/`; `review-ideas` and the user
move ideas to `ready/`; `advance-plans` builds `ready/` and the pre-approved ideas.

```
measure ──► triage ──► investigate ──────────► select ──► verify ─────────────► land
(script)    units.json  drafts/<unit>.md+.json   final/    1 skeptic per plan   commit, report
```

The stages talk only through files in a **work directory** (`<work>`), so they run the
same in one session or as separate agents started by a workflow (the workspace
`/enhance` does that). Each agent then needs only its stage's section below. Every extra
agent pays an onboarding cost, so a small repo runs all stages in one agent, and only a
large repo with three or more independent units gets one investigator per unit.

## Repo facts this skill reads — never guess them

| Fact | Where |
|---|---|
| commit format; whether docs-only commits need a PR (default: no — straight to `<base>`), and if so its labels and review loop | the repo's `AGENTS.md`, `.agents/land.config.json` |
| what agents may start without asking | `AGENTS.md` `## Approval` (absent → nothing is pre-approved) |
| hard stops | `.agents/land.config.json` `hardStop`, or the list in `AGENTS.md` |
| **what to look for and what never to propose** | `AGENTS.md` `## Enhance` (optional, below) |
| source dirs, test harness, domain invariants | `## Enhance`, else `AGENTS.md`, code maps and the domain skills it names |
| base branch | `.agents/land.config.json` `baseBranch`, else `origin/HEAD`, else `origin/main` |
| where worktrees live | the caller's scratch path if it gives one — this beats an in-repo location in `AGENTS.md` — else `AGENTS.md`, else a scratch dir outside the repo |
| plan validator | `scripts/plans-map.js --validate` if present; in a record repo, also its own health check |
| plans outside the repo that cover it | the workspace's cross-repo plans, any upstream convention repo `AGENTS.md` names |
| trailers the plan skills query (`Reviewed-Idea:`, `Pre-Approved:`) | only if `review-ideas`/`advance-plans` are wired, or `AGENTS.md` names them |

### The `## Enhance` section

A repo tunes the pass with a short section in its `AGENTS.md` — data, not procedure.
Every line is optional; a repo without the section gets the defaults.

```markdown
## Enhance

- **Source / tests:** <dirs> · <test harness and how to run it>
- **Invariants a design must keep:** <the domain skills or docs that hold them>
- **Lenses:** + <extra lens> · − <lens this repo does not want>
- **Never propose:** <shapes or areas that are off the table, and why>
- **Gates:** <paths whose change needs a gate, and which one>
```

## Arguments

| Argument | Meaning | Default |
|---|---|---|
| a path, domain or named area | **scoped**: measure and triage only there; the user already said it hurts, so low churn is no reason to drop a unit | whole repo |
| `--n <k>` | at most `k` plans | 3 (scoped: 2) |
| `--lens <name>[,…]` | only these lenses | every lens the repo's kind allows, adjusted by `## Enhance` |
| `--dry-run [<out>]` | plans go to `<out>/<repo>/` (default `<work>/out/`), no worktree, nothing committed; the report still comes back | off |
| `--work <dir>` | the work directory | a new scratch dir |
| `--stage <triage\|investigate\|select> [--unit <id>]` | run one stage only (set by a workflow) | all stages, in order |

`N` is a ceiling, never a quota. Zero plans with a good reason is a correct result; filler
is not.

## Rules for every stage

- `AGENTS.md` wins over this file wherever they differ. Read it first, then only what
  shapes planning: code maps, architecture docs, invariants. Build-time procedure is read
  only when a unit touches its domain.
- **Read-only** outside `<work>`: `fetch`, `status`, `log`, `show`, `diff`, and reads.
  Only select & land writes to the repo, and only in a worktree (or `<out>` in a dry run).
  No `reset`, `commit --amend`, `checkout --`/`restore`/`clean`, `rebase`, `add -A`/`add .`,
  hook-bypass flags or `worktree remove` of anything you did not create; report BLOCKED
  instead.
- **Budget.** Never block on a slow helper. Write what the evidence already justifies, and
  report the rest as `PENDING`. A pass that files two good plans beats one that files none.
- Starting helpers yourself: in **one message**, **in the foreground**
  (`run_in_background: false`). A backgrounded helper reports to the top of the session,
  not to you, and its findings are lost to this pass.

## 1. Measure — a script, not an agent

From the repo root, after `git fetch origin`:

```bash
node <agent-skills>/scripts/enhance-measure.js [--scope <path>] [--since 365] > <work>/evidence.json
```

(`scripts/enhance-measure.js` in a repo that consumes agent-skills; otherwise the path to
an agent-skills checkout.) It gives, for `origin/<base>`: the hottest source files by
churn × size × fix density with rename-folded history, their fix commits, co-change
partners (flagged when in another module), whether a test is named after them; churn per
module; commit style; size signals and `fanoutEligible`; the repo's `## Approval`,
`## Enhance` and hard stops; every plan, decision and incident by title; open PRs with
their files. Widen `--since` when the window holds few commits.

## 2. Triage — cut the repo into units

One agent, reading `evidence.json` — not the whole repo. It adds only the cheap evidence
the script cannot produce, per lens:

- **architecture** (code): the hot files and cross-module co-change are the candidates.
  Look for shallow modules, one concept spread over many files, the same rule or threshold
  in several places, logic tested only through mocks.
- **reliability** (code): `gh run list -L 50` (failure rate per workflow, reruns of one
  SHA); incident titles ("what still bites" lives there); hot files with no named test;
  manual deploy steps; secrets and config hazards.
- **product** (code or record): the repo's stated purpose, its plans and decisions,
  `gh issue list --state open`, what recent commits keep circling. Be bold — "interesting"
  beats "safe" — but anchor every idea in something the repo does, wants or struggles with.
  For an infra repo, product means what its operator can do.
- **record** (record): run the repo's own health check first. Then **dated obligations the
  record knows about that no plan tracks** (the most valuable find), the same fact
  disagreeing across files, gaps against the perimeter, manual capture worth automating.

Then **cut coherently**:

- **A unit is one concept** — a cluster, a gap, a capability — never a file and never a
  bundle. Name it with the slug its plan would get.
- **Merge** candidates that are one change seen twice. **Split** a refactor from a
  behaviour change: they get different approvals. Record the order between related units
  (`after: <id>`), not as a gate.
- **Drop at the door** what the backlog titles or open PRs already cover, what
  `## Enhance` says never to propose, and code a ready/ongoing plan moves out of the repo
  (`NOTE` it for the destination).
- **Bounds:** at most `2 × N` units. Fewer is fine; none is fine with a reason.
- **Fan out** (one investigator per unit) only when `evidence.size.fanoutEligible` is true
  and there are three or more units; otherwise investigate inline. Write why either way.

Write `<work>/units.json`:

```json
{
  "kind": "code", "lenses": ["architecture", "reliability", "product"],
  "fanout": true, "why": "<size reasons>; <k> independent units",
  "units": [
    { "id": "<plan slug>", "lens": "architecture",
      "concept": "<the one concept, in a line>",
      "evidence": ["<file> churn <n>, <lines> lines", "<sha> <fix subject>"],
      "paths": ["<files it would touch>"],
      "check": "<what the investigator must verify first: a decision, an overlapping plan>",
      "after": null }
  ],
  "dropped": [{ "candidate": "<…>", "reason": "<covered by / moving out / never-propose>" }],
  "urgent": [], "stale": [], "notes": []
}
```

## 3. Investigate — one unit, in depth

One agent per unit when fanning out, or the triage agent itself, unit by unit. Each gets
only its unit, `evidence.json`, and the rules above.

1. **Dedup in depth:** grep `docs/plans/` and `docs/decisions/` for the unit's concepts and
   read what matches; check `openPrs` for its paths. Already planned → drop (if that plan's
   facts are wrong, write the corrected copy to `<work>/fixed/<plan file name>`). A decision
   forbids the shape → drop, or address it. An open PR touches the same files → keep it,
   gated `blocked:PR #<n> (<why>)`.
2. **Explore:** why it is coupled, fragile or missing — path + symbol, the fix commits —
   its dependency category (table below), its test impact.
3. **Design:** architecture units sketch three shapes — **A** minimal interface, **B** the
   common caller trivial, **C** ports & adapters if it crosses a runtime or network
   boundary, else most extensible — and pick one or a better hybrid. Other lenses: the
   chosen approach and its strongest alternative. A mechanical change or a shape the repo
   already prescribes gets one line on alternatives. No design clearly better than today →
   drop.
4. **Draft** the plan to `<work>/drafts/<id>.md` in the format of §5, and write
   `<work>/drafts/<id>.json`:

```json
{ "id": "…", "verdict": "file | drop", "reason": "…", "lens": "…",
  "priority": "high | medium | low", "value": 4, "valueWhy": "one line",
  "class": "pre-approved | needs-yes", "clause": "quoted § Approval clause, if pre-approved",
  "question": "…", "recommended": "…", "hardStop": false,
  "urgent": [], "stale": [], "notes": [] }
```

`value` (1–5) is what the select stage ranks by: cost saved or risk removed for
architecture and reliability; how far it moves the repo's purpose for product and record.

## 4. Select and classify — the join

One agent reads `units.json` and every `drafts/*.json`, and opens the drafts it keeps.

- **Drop:** cosmetic friction (not in scoped mode); a domain-invariant conflict you cannot
  resolve; an honest benefit of "nicer".
- **Merge** drafts that turned out to be one change; keep the better-evidenced one.
- **Keep at most `N`**, by `value`, preferring a mix of lenses when values are close.
- **Classify** each, rereading it in full:

| The plan… | Class |
|---|---|
| is freed by a clause in `## Approval` (typically: changes nothing observable; a migration that follows from an architectural benefit; consolidating copies that agree) | **pre-approved** — quote the clause |
| changes what a user sees or can do, a product rule, a permission, a contract, stored data shape beyond a following migration, running cost; or the repo has no `## Approval`; or you are unsure | **needs a yes** — `**Gate:** decision:<the single question>` |
| mixes both | split it if the pre-approved half is useful alone; otherwise it needs a yes |

No `## Approval` → everything needs a yes; skip the analysis. A needs-yes idea always
carries its `decision:` gate. A pre-approved plan has no open questions. Paths that hit a
hard stop → say so in its Approval section (its PR never self-merges). Add the `blocked:`
gates `## Enhance` or the repo's rules call for.

Write each kept plan, in the §5 format, to `<work>/final/<slug>.md`.

## 5. The plan format

`docs/plans/ideas/<slug>.md`, bare kebab-case, in the language the repo's plan bodies use,
block lines formatted as its existing plans format them. `Priority` required (`high` = hot
path or repeat fixes, `medium` = known pain, `low` = cleanup); `Gate` only when set; never
`Stage`, `Status`, `Updated`, `Next` or `Landed` on an idea.

```markdown
# <Title>

**Priority:** high | medium | low — <one-line rationale from the evidence>
**Gate:** <only if set>

**Goal:** <one sentence: what changes, and what it stops costing or starts enabling>

## Context
The evidence, as of `<short sha of origin/base>` (<date>): numbers with their window; short
SHA + subject of the commits that hit it; path + symbol, never bare line numbers.

## Design
The chosen shape by responsibility, an illustrative signature or example (say so), how
callers or users migrate. Which related plan builds first, if any.
**Alternatives considered:** one line each, and why they lost.
**Testing strategy:** (code) tests at the new boundary, and the shallow tests they make
redundant — replace, don't layer.

## Paths
Every file it would touch, as of <date>; `(new)` for files that don't exist yet, a
directory where the files aren't knowable yet.

## Approval
Pre-approved — "<quoted clause>". | **Needs your yes** — the `Gate` question; recommended
answer: <answer>, because <one line>.

## Out of scope
What it deliberately does not do.
```

Approve early, plan late: the decision and the evidence; the File Structure and Tasks are
written by whoever moves it to `ongoing/`, against that day's code.

## 6. Verify — one skeptic per plan

Plans read as solid and still carry false claims: on their first landing, a reviewer
caught a health endpoint known to lie and a wrong statement of how a tool behaves, both
written with confidence. So before anything lands, **each plan in `<work>/final/` gets an
agent that did not write it**, whose job is to refute it:

- Check every factual claim against `origin/<base>` at `evidence.json`'s `sha`: paths and
  symbols exist and say what the plan says; SHAs exist and their subjects match; counts
  are right; statements about how a tool, framework or service behaves are true (read the
  code or the docs, never memory); the chosen design does not reproduce a failure the
  repo has documented (incidents, comments next to the code, decisions).
- **Fix in place** what is wrong but not load-bearing. A premise that turns out false — the
  problem does not exist, or a plan, PR or decision already covers it — **drops** the plan.
- Return `{ slug, verdict: keep | corrected | drop, corrections: [], reason }`.

In one session, start the skeptics in one message, in the foreground; without subagents,
re-read each plan yourself as its adversary before landing. Never skip this stage to save
tokens: one wrong plan built on a false premise costs more than every skeptic.

## 7. Land

In `--dry-run`, the verified plans stay in `<work>/final/` (and corrections in
`<work>/fixed/`); report that directory.

Otherwise, in a detached worktree of `origin/<base>`: copy the verified plans from
`<work>/final/` into
`docs/plans/ideas/` and the corrected plans over their originals (after checking each
original is unchanged since `evidence.json`'s `sha`). Run the plan validator if there is
one (`node scripts/plans-map.js --validate; V=$?; [ $V -eq 0 ] || exit $V` — never through a
pipe; none → say so). **One** commit, staged by explicit path, its subject in the repo's
format:

```
<subject in the repo's format>: enhancement pass — <n> ideas (<k> pre-approved, <m> need a yes)

<one line per plan: slug · lens · class>

Reviewed-Idea: <slug>          (only where the repo uses these trailers)
Pre-Approved: <slug>           (only for pre-approved plans)
Co-Authored-By: <the session's attribution line>
```

Trailers go in the final paragraph. The commit touches only Markdown, so it goes
**straight to `<base>`**, with no PR: plans are documents, not code a reviewer must gate.
Open a PR only when `AGENTS.md` explicitly requires one for docs-only changes, and then
follow its whole PR procedure — every label, every review loop — and never merge it. Never
commit a generated plans map. Remove the worktree.

## 8. Report

```
REPO <repo>   KIND <code|record|mixed>   LENSES <…>   UNITS <n> (<fanout|inline>: <why>)
URGENT <what> | <date> | <why it is at risk now>   (first, always)
FILED <slug> | <lens> | <priority> | pre-approved|needs-yes | <one line>
QUESTION <slug> | <the question> | recommended: <answer>
DROPPED <candidate> | <reason>
FIXED <plan> | <stale fact corrected>
STALE <file[:line]> | <stale fact outside the plans, not edited>
NOTE <what> | <why the user should know>
PENDING <unit> | <what was unchecked when the pass stopped>
LANDED <sha on base> | PR <url> | DRY-RUN <dir> | NOTHING
BLOCKED <step> | <why>
```

An `URGENT` finding is reported, not acted on. Talking to a person rather than a parent
agent: the same content as prose — questions first, each answerable with a yes.

## Dependency categories

| Category | Description | Deepening recipe |
|---|---|---|
| **In-process** | Pure computation, in-memory, no I/O | Merge the modules, test directly. |
| **Local-substitutable** | Has a local stand-in (emulator, in-memory DB, test container) | Deepen with the stand-in in the harness; real semantics, not mocks. |
| **Remote but owned** | Your own code across a runtime boundary (API, job, other service) | A port at the boundary; real adapter in prod, in-memory adapter in tests. |
| **True external** | Third-party APIs you don't control | Mock at the boundary; inject the port. |

Watch for an owned store treated as "external" and over-mocked when a local stand-in exists.

## Common mistakes

| Mistake | Instead |
|---|---|
| Re-deriving churn with `git log` pipelines | `enhance-measure.js` already did it |
| Landing a plan nobody tried to refute | §6: one skeptic per plan, always |
| Reading the whole repo in triage | Triage reads `evidence.json`; investigators read deeply |
| One unit per file, or one unit holding three ideas | One concept per unit |
| Fanning out a small repo | Inline below `fanoutEligible`; onboarding a helper costs more than it saves |
| Filing N because N was the number | N is a ceiling; zero is fine |
| Filing what the backlog already has | Dedup at triage by title, at investigation in depth |
| Asking the user to pick a unit or a design | Decide; record the losers in the plan |
| `Pre-Approved` with no `## Approval` clause, or with an open question | `decision:` gate |
| Filing into `ready/`, writing code, opening a code PR | `ideas/`, plans only |
| Bundling a refactor with a behaviour change | Two units, two plans |
