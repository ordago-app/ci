# agent-skills

The autonomous delivery contract (`ship-a-feature`), the landing state machine behind it
(`scripts/pr-land.js`), and the parallel-batch layer on top (`orchestrate`, `advance-plans`,
`review-ideas`, with the plans map, per-worktree slots and fleet scripts they need), consumed by
multiple projects as a git submodule so a single copy is the source of truth.

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
- **orchestrate** — one leader session supervising several worker sessions (tmux windows +
  worktrees) to landed PRs: one decision with the user, durable batch state, an escalation
  contract. Each worker runs the `ship-a-feature` loop.
- **advance-plans** — an `orchestrate` batch that builds everything already approved —
  `ongoing/`, then `ready/`, then ideas the repo's `## Approval` policy pre-approves — and
  whose finish line is the map, not a PR count: no `go`, the leader dispatches and asks only
  for unlocks and product questions, until nothing agents can move is left.
- **review-ideas** — checks `docs/plans/ideas/` against the code with read-only scouts,
  oldest-reviewed first, fixes or retires what is false, and asks the user for the yeses
  that move ideas to `ready/`. Docs only; no PRs except one for ideas pinned by code.
- **enhance-repo** — an autonomous enhancement pass over one repo: `enhance-measure.js`
  measures it, one agent triages the evidence into coherent units, the units are
  investigated (in parallel on a large repo), and the best N become `ideas/` plans,
  pre-approved only where `## Approval` frees them. A repo tunes it with an optional
  `## Enhance` section in its `AGENTS.md`. Plans only; feeds `review-ideas` and
  `advance-plans`.

**Base sections**

- **base/** — the agent guidance several repos share word for word (repo health, the
  don'ts, be proactive, the autonomy opening), one section per file. A repo copies a section
  into its `AGENTS.md` between `<!-- base:NAME -->` markers, so its clone alone carries the
  text. See [`base/README.md`](base/README.md).

**Scripts**

- **scripts/pr-land.js** — the landing state machine behind `pnpm pr:land`. Shared verbatim;
  every repo-specific value is data in that repo's `.agents/land.config.json`.
- **scripts/plans-map.js** — generates `docs/plans/_plans-map.md` from the metadata block
  that [agent-plans](https://github.com/alvaro-francisco-gil/agent-plans) v2 puts under every
  plan's title, and validates those blocks. The leader's first read.
- **scripts/agent-env.sh** (+ `lib/agent-slots.sh`) — gives each agent worktree its own
  slot: a block of 100 ports, a `firebase.agent.json` with every emulator moved into it, and
  a one-time setup (submodules, dependency install). Without it, two workers' emulator test
  runs silently evict each other.
- **scripts/agent-capacity.js** — admit one more worker or not (exit 0/2), from free RAM,
  running emulator suites and, if declared, a self-hosted CI queue. Replaces a worker count.
- **scripts/agent-dispatch.sh** — one worker: worktree off a fetched `origin/<base>` with its
  submodule, its own `<project>-fleet` tmux window, `claude` launched in auto mode with peer
  messages accepted, startup dialogs answered, the run confirmed started.
- **scripts/agent-reap.sh** — reaps one finished worker: refuses (exit 2) unless its worktree is
  clean and its HEAD is on `origin/<base>`, then stops its lander, frees its slot, closes its
  window, removes the worktree, prunes, and `branch -d`. Never `worktree remove --force`.
- **scripts/pr-land-bg.sh** — runs `pnpm pr:land` in a tmux session that survives, and
  stops only this worktree's lander (`--kill`), never another's.
- **scripts/ideas-review-order.js** — ideas by last review, oldest first, from git history
  and `Reviewed-Idea:` trailers. `review-ideas`' queue.
- **scripts/enhance-measure.js** — `enhance-repo`'s evidence as JSON: churn × size × fix
  density with rename-folded history, co-change, the backlog by title, `## Approval` /
  `## Enhance`, hard stops, open PRs and the size signals that decide fan-out. Read-only.
- **scripts/agent-auto-mode.js** — installs the repo's `.agents/auto-mode.json` policy into
  the user's Claude Code settings (see *Auto-mode policy*).

**Hooks**

- **hooks/guard-lander-kill.sh** — a `PreToolUse(Bash)` hook refusing pattern-wide kills
  (`pkill -f`, `killall`, `pgrep -f … | xargs kill`) that name a lander. One such kill
  takes out every worktree's `pr:land` on the machine.

**Templates**

- **templates/plans-map.yml** — the CI workflow that validates plan blocks on PRs and
  regenerates the map on the base branch. Copied, not linked: a workflow cannot be a symlink.

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
git submodule add https://github.com/ghostaff-ai/agent-skills.git .agents/_shared
ln -s ../_shared/skills/ship-a-feature .agents/skills/ship-a-feature
ln -s ../.agents/_shared/scripts/pr-land.js scripts/pr-land.js
git add .gitmodules .agents/_shared .agents/skills scripts/pr-land.js
```

Then add `"pr:land": "node scripts/pr-land.js"` to `package.json`, and write
`.agents/land.config.json` (see below).

### Vendoring `pr-land.js` instead of the submodule

A repo whose production builds clone submodules (Vercel does, during every build) should
not depend on this personal repository: renaming it or making it private would break that
repo's deploys. Copy `scripts/pr-land.js` and `scripts/decide.js` (and their
`__tests__/`) into the repo, e.g. `scripts/pr-land/`, with a header naming the source
commit, and point `pr:land` at the copy. Update by copying the newer files over and
re-running the tests. The config file and every rule below apply unchanged.

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
  "ciGates": [
    {
      "workflow": "ci.yml",
      "paths": ["src/", "functions/", "package.json", "pnpm-lock.yaml"],
      "requiredLanes": ["Lint + Unit"]
    },
    { "workflow": "console-ci.yml", "paths": ["apps/console/"], "requiredLanes": ["Console unit"] }
  ],
  "sharedBlastRadius": ["packages/shared/", "pnpm-lock.yaml"],
  "integrationCheck": { "command": "pnpm -s typecheck" },
  "hardStop": [
    { "pattern": "^firestore\\.rules$", "why": "security rules" },
    { "pattern": "^scripts/backfills/", "why": "data backfill" }
  ]
}
```

- `ciGates` has **one entry per path-filtered CI workflow**, and each entry's `paths` **must
  mirror that workflow's path filter** (`["**"]` for a workflow with none). If they drift,
  the vacuous-green guard either blocks needlessly or — worse — reads "no run dispatched" as
  "tests passed". A diff matching no gate is marked UNVERIFIED and rests on review alone.
- `requiredLanes` are exact check names (as `gh pr checks` prints them) that must reach
  SUCCESS: SKIPPED counts as failed, and not-yet-reported counts as pending. They are
  enforced **only when their own gate matches the diff** — that is why they live per gate.
  One list for the whole repo would demand workflow A's lane on a diff that only dispatches
  workflow B, and every such PR would wedge. List only a job that runs whenever its workflow
  dispatches at all (no job-level `if:`, no `needs:` on a job that can be skipped), or a
  legitimate skip wedges the PRs it applies to.
- Why per workflow rather than one `ciPaths` list: with two path-filtered workflows, a diff
  touching only the second one's paths read as UNVERIFIED and merged on review while that
  workflow was still running. The legacy top-level `ciPaths` + `requiredLanes` are still
  accepted and read as a single gate; declaring both shapes is rejected at load.
- `mergeMethod` is `"merge"`, `"squash"` or `"rebase"` — match whatever the repo's history
  already does, since the loop is not the place to change it. An unrecognised value is
  rejected when the config loads, not at the merge.
- `sharedBlastRadius` names paths whose movement on the base matters to every PR (a
  shared package, the lockfile). With no `integrationCheck`, such a move forces a rebase —
  every CI lane and a fresh review, for every open PR, on every merge. Set
  `integrationCheck` (`{ "command": "…", "timeoutMs": 900000 }`) and `pr:land` instead
  builds the merge result in a throwaway worktree and runs that command there, once the PR
  is green and approved. That worktree, and the command's `TMPDIR`, live on disk under
  `$PR_LAND_SCRATCH_DIR`, else `$XDG_CACHE_HOME/pr-land`, else `~/.cache/pr-land` — never
  in `/tmp`, which is often RAM — and are deleted after the run. Keep it to what a shared-code move can break without touching the
  PR's files — typecheck and fast unit tests, not the full suite. A change to the *same
  file* on both sides still rebases.
- **A rebase is only ever triggered by something a rebase removes**, or the loop can never
  land a PR on a base that moves faster than its CI:
  - the same file changed on both sides, or the **base** moved a `rebaseRadius` path
    (paths only the full CI can judge — security rules, root manifests) → rebase, early;
  - the **PR** changes a `rebaseRadius` path → `integrationCheck` at scope `wide`;
  - `sharedBlastRadius` changed on either side → `integrationCheck` at scope `shared`.

  "Either side" counts whenever the other side moved anything — not only what a `ciGates`
  entry covers, because a consumer with no gate of its own sits outside every filter while
  importing the shared code all the same. The command receives the scope as `PR_LAND_INTEGRATION_SCOPE`
  (`shared` | `wide`) and decides what each covers; `wide` should reach every workspace a
  `rebaseRadius` path can break. A passing verdict is kept per head and holds while the
  base moves only outside the PR's files and `rebaseRadius` — shared code moves on most
  merges, and voiding the verdict for it would starve any check longer than the gap
  between them; a shared move that lands mid-check is left to the base's own post-merge
  CI. A verdict older than `maxAgeMs` (default 30 min) answers nothing. A hard-stopped PR is
  handed over before the check runs. Checks run one at a time per clone (parallel workers
  share it), and a command that exits **3** — or times out — is recorded as *could not run*
  and handed to a human, never reported as a broken merge.
- With no `integrationCheck`, only the **base** side rebases; the PR's own side of a radius
  is not verified before the merge at all, because a rebase cannot remove that trigger.
- **A merge queue on the base replaces all of the staleness rules above.** `pr:land` reads
  `isMergeQueueEnabled` off the PR — there is no config key, because a base that requires a
  queue accepts nothing else. With one, it never rebases and never runs `integrationCheck`:
  a green, approved, ungated PR is enqueued (`enqueuePullRequest`, pinned to the head it saw
  green), and GitHub runs CI on the PR stacked on the base and merges a green result. A
  removal is retried once (a single flaky lane), a second removal of the same head exits
  `10` with the queue's reasons, and `queueTimeoutMs` (default 90 min, counted from entering
  the queue) bounds the wait. A conflict still needs a rebase. The repo's side: a ruleset
  requiring the queue, and a `merge_group:` trigger on the workflows whose checks it requires
  — without that trigger the queue waits for checks that never report.
- A **round** is a `CHANGES_REQUESTED` review, and `maxReviewRounds` caps those alone. An
  approval ends the conversation, so the review a later CI-fix push buys is not a round —
  and neither is an approval the reviewer re-posts onto a clean rebase.
- `hardStop[].pattern` is a regex *source string*, not `/slashes/`; add `"flags": "i"` if needed.
- `requireApprovingReview: false` is a **weaker** bar, not an equivalent one. Set it only where
  no automated reviewer exists, and expect `ship-a-feature` to say so out loud.
- With no reviewer bot, also set `"reviewLabel": null`, so the loop does not create a label
  nothing listens to.

### Adding the enhancement pass

`enhance-repo` needs nothing else wired:

```sh
ln -s ../_shared/skills/enhance-repo .agents/skills/enhance-repo
ln -s ../.agents/_shared/scripts/enhance-measure.js scripts/enhance-measure.js
```

Optionally add a `## Enhance` section to `AGENTS.md` (format in the skill).

### Adding the orchestration layer

`orchestrate`, `advance-plans` and `review-ideas` sit on top of `ship-a-feature`, so wire
that first. Then:

```sh
for s in orchestrate advance-plans review-ideas; do ln -s ../_shared/skills/$s .agents/skills/$s; done
for f in plans-map.js agent-env.sh agent-capacity.js agent-dispatch.sh agent-reap.sh pr-land-bg.sh \
         ideas-review-order.js agent-auto-mode.js; do
  ln -s ../.agents/_shared/scripts/$f scripts/$f
done
mkdir -p .claude/hooks && ln -s ../../.agents/_shared/hooks/guard-lander-kill.sh .claude/hooks/guard-lander-kill.sh
cp .agents/_shared/templates/plans-map.yml .github/workflows/plans-map.yml   # set the base branch
echo 'firebase.agent.json' >> .gitignore
```

Add to `package.json`: `"plans:map": "node scripts/plans-map.js"`,
`"agent:capacity": "node scripts/agent-capacity.js"`,
`"agent:dispatch": "bash scripts/agent-dispatch.sh"`,
`"agent:reap": "bash scripts/agent-reap.sh"`,
`"agent:auto-mode": "node scripts/agent-auto-mode.js"`. Register the hook in
`.claude/settings.json` as a `PreToolUse` hook with matcher `Bash` and command
`$CLAUDE_PROJECT_DIR/.claude/hooks/guard-lander-kill.sh` (it needs `jq`). Then:

1. **`.agents/orchestrate.config.json`** — the fleet facts the skills and `agent-env.sh` read:

   ```json
   {
     "project": "myrepo",
     "maxWorkers": 4,
     "maxConcurrentEmulatorSuites": 2,
     "worktreesDir": ".claude/worktrees",
     "worktreeSetup": ["pnpm install --frozen-lockfile --prefer-offline"],
     "ciCapacity": "one line: what limits how many PRs CI can admit at once"
   }
   ```

   - `project` names the leader sessions (`<project>-orchestrator`, `<project>-build`) and
     the tmux session (`<project>-fleet`). Without the file the skills refuse to start.
   - `capacity` (optional) tunes `agent-capacity.js`: `minAvailableMb` (default 3000), and
     `ciQueue: { "runnerLabel": "self-hosted", "maxMinutes": 30 }` when CI runs on
     self-hosted runners whose queue should hold dispatches back. Hosted runners: omit it.
   - `worktreeSetup` runs once per fresh worktree, in it, after submodule init. A fresh
     worktree has no `node_modules`; **never symlink the main checkout's in** — a workspace
     package link is relative, so the worktree would silently test the main checkout's source.
   - `maxConcurrentEmulatorSuites` is the machine's ceiling, measured, not guessed (two on a
     16 GB WSL2 host).

2. **Plan metadata blocks** per agent-plans v2 (`**Priority:**` on every plan;
   `**Gate:** / **Next:**` in `ongoing/`). If the repo deploys, add the exact line
   `<!-- plans:landed -->` to its `AGENTS.md`, and every ongoing plan then also needs
   `**Landed:**`. Run `node scripts/plans-map.js --validate` until it passes, then
   generate the map once and commit it.

3. **Make the emulator test harness slot-aware.** `agent-env.sh` writes
   `firebase.agent.json` beside `firebase.json`; the harness must start the emulators from
   that file when it exists and point the tests at its ports. The file — not an exported
   variable — carries the slot, because agent shells (Claude Code's Bash tool among them) do
   not keep environment variables between commands. Any test that hardcodes a port instead
   of reading `FIRESTORE_EMULATOR_HOST` and friends has to change too.

4. **An `## Approval` section in `AGENTS.md`** — what agents may start without asking
   (pre-approved idea classes), what needs the user's yes, and which environments only the
   user may write to. `advance-plans` builds pre-approved ideas from it, `review-ideas`
   classes ideas by it, and both treat a repo without it as "nothing is pre-approved".

5. **The regenerate job pushes to the base branch** as `github-actions[bot]`. If that branch
   is protected against direct pushes, allow the bot, or the map never updates.

Verify:

```sh
node --test .agents/_shared/scripts/__tests__/*.test.mjs
node scripts/plans-map.js --validate
(cd .claude/worktrees/<any> && source scripts/agent-env.sh)   # prints the slot's ports
```

### Auto-mode policy

Workers run `claude --permission-mode auto`, so Claude Code's permission classifier judges
every command they and the leader run. It reads `autoMode` only from user, `--settings`
and managed settings — **never from a repo's own `.claude/settings*.json`**, since a cloned
repo must not be able to widen its own permissions. So the repo states its policy in
`.agents/auto-mode.json`, and each person who runs agents installs it:

```json
{
  "tag": "myrepo",
  "allow": ["Sending keystrokes with `tmux send-keys` to windows of the `myrepo-fleet` tmux session is allowed: …"],
  "soft_deny": [],
  "environment": ["**Trusted repo**: github.com/me/myrepo … `develop` is the integration branch …"]
}
```

```sh
pnpm agent:auto-mode            # show what would change in ~/.claude/settings.json
pnpm agent:auto-mode --write    # back it up, then apply
```

Every entry is installed prefixed `[<tag>] ` (default: the repo's directory name), and a
re-run replaces exactly that repo's entries — several repos' policies and the person's own
rules coexist. Keep the file a restatement of `AGENTS.md`'s `## Approval` and autonomy
rules, never a looser one: the classifier is what enforces them for an unattended fleet.

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
