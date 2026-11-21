# agent-skills

The autonomous delivery contract (`ship-a-feature`) and the landing state machine behind
it (`scripts/pr-land.js`), consumed by multiple projects as a git submodule so a single
copy is the source of truth.

> **`managing-plans-lifecycle` has moved** to
> [agent-plans](https://github.com/alvaro-francisco-gil/agent-plans), where it ships as a
> plugin for Claude Code, Codex, Cursor and Gemini. It left because it is pure convention
> — folders and `git mv`, no scripts — so it installs anywhere, while `ship-a-feature`
> needs `pr-land.js`, a `pr:land` npm script and `.agents/land.config.json`, none of which
> a plugin can install for you. That is why this repo stays a submodule and is no longer a
> plugin marketplace.

## What's here

**Skills**

- **ship-a-feature** — the autonomous delivery contract: front-load every question into ONE
  message, take `go` as "all your picks", then implement and land unattended. Supersedes
  `superpowers:brainstorming`'s one-question-per-message rule and
  `finishing-a-development-branch`'s stop-and-ask merge menu in adopting repos.

**Scripts**

- **scripts/pr-land.js** — the landing state machine behind `pnpm pr:land`. Shared verbatim;
  every repo-specific value is data in that repo's `.agents/land.config.json`.

## The layering rule

These files define *procedure*. Every project fact — which paths are Direct, what the
hard-stop list is, the base branch, whether an approving review is required — lives in the
consuming repo's `AGENTS.md` and `.agents/land.config.json`, **which win wherever they
differ.** If you are about to edit a file here to make one repo happy, that value belongs in
that repo's config instead.

## How consuming repos wire it in

The submodule lives **outside** the skill-scan directory (so it doesn't show up as a bogus
skill), and each shared skill is symlinked into `.agents/skills/` at depth 1:

```
.agents/_shared/                            # this repo, as a submodule
   skills/ship-a-feature/SKILL.md
   scripts/pr-land.js
.agents/skills/ship-a-feature            →  ../_shared/skills/ship-a-feature
.claude/skills                           →  ../.agents/skills
scripts/pr-land.js                       →  ../.agents/_shared/scripts/pr-land.js
```

Consumers point one level deeper than the repo root, into `skills/`.

### Add to a new repo

```sh
git submodule add https://github.com/alvaro-francisco-gil/agent-skills.git .agents/_shared
ln -s ../_shared/skills/ship-a-feature .agents/skills/ship-a-feature
ln -s ../.agents/_shared/scripts/pr-land.js scripts/pr-land.js
git add .gitmodules .agents/_shared .agents/skills scripts/pr-land.js
```

Then add `"pr:land": "node scripts/pr-land.js"` to `package.json`, and write
`.agents/land.config.json` (see below).

### `.agents/land.config.json`

Required in every consuming repo. Defaults **fail closed** — an absent config means an empty
hard-stop list *and* `requireApprovingReview: true`, so an unconfigured repo stalls at the
review gate rather than silently auto-merging something it never declared.

```json
{
  "baseBranch": "develop",
  "reviewLabel": "ai-review",
  "requireApprovingReview": true,
  "mergeMethod": "merge",
  "ciPaths": ["src/", "functions/", "package.json", "pnpm-lock.yaml"],
  "sharedBlastRadius": ["packages/shared/", "pnpm-lock.yaml"],
  "hardStop": [
    { "pattern": "^firestore\\.rules$", "why": "security rules" },
    { "pattern": "^scripts/backfills/", "why": "data backfill" }
  ]
}
```

- `ciPaths` **must mirror the repo's CI path filter.** If they drift, the vacuous-green guard
  either blocks needlessly or — worse — reads "no run dispatched" as "tests passed".
- `mergeMethod` is `"merge"`, `"squash"` or `"rebase"` — match whatever the repo's history
  already does, since the loop is not the place to change it. An unrecognised value is
  rejected when the config loads, not at the merge.
- `hardStop[].pattern` is a regex *source string*, not `/slashes/`; add `"flags": "i"` if needed.
- `requireApprovingReview: false` is a **weaker** bar, not an equivalent one. Set it only where
  no automated reviewer exists, and expect `ship-a-feature` to say so out loud.

### Wiring the reviewer: a public repo cannot use the immediate trigger

Some repos here call a private homelab reusable workflow to poke the reviewer the moment
CI goes green, instead of waiting for its ~15-minute poll. **That is only available to a
private caller.** A public repo cannot call a private repo's reusable workflow, and the
failure is not the one you would design for:

- GitHub resolves the callee when it **creates the run**, before evaluating any job-level
  `if:`. Gating the job behind an unset variable does not make it inert.
- The run then completes with **zero jobs** — every real test in that workflow is skipped
  too, and the PR shows no checks rather than a failing check.

Seen on 2026-08-22 in two public repos (cultuvilla run `32594475090`, lectoemocion-platform
run `32594747047`), both from a job believed to be inert. Public repos use the poll backstop,
which runs entirely on the reviewer's side and needs only a registry entry — latency, not
capability. Check `gh api repos/OWNER/REPO --jq .visibility` before adding the job.

### Verify the wiring

```sh
node --test .agents/_shared/scripts/__tests__/pr-land.test.mjs   # shared predicates
pnpm pr:land --dry-run                                          # from a feature branch
```

### Clone / CI

Submodule contents are not fetched by a plain `git clone`. Use:

```sh
git clone --recurse-submodules <repo>
# or, in an existing checkout:
git submodule update --init
```

### Update to the latest shared version

```sh
git submodule update --remote .agents/_shared
git add .agents/_shared && git commit -m "chore: bump agent-skills"
```

## Why a submodule and not a plugin

`ship-a-feature` cannot ship as a plugin. Its central step is `pnpm pr:land`, which runs
`scripts/pr-land.js` and reads `.agents/land.config.json` — a plugin installs skills, not
npm scripts or repo config, so an installer would get a skill whose main step calls a
command that does not exist. The submodule carries the script and the skill together, pins
a SHA per repo, and is visible to CI (`submodules: true`) and to Codex, which reads
`.agents/skills/` directly.

`managing-plans-lifecycle` had the opposite shape — folders and `git mv`, nothing to
install — so it moved to [agent-plans](https://github.com/alvaro-francisco-gil/agent-plans)
and ships as a plugin there.

A repo can use both: the submodule for `ship-a-feature`, the plugin for the plans
lifecycle. They no longer overlap, so nothing loads twice.
