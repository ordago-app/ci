# ordago-app/ci — Agent Notes

The CI platform for a **federated pool shared by two operators**. It polls each
organisation's GitHub job queue, decides where work runs against one global
ledger, and spawns ephemeral runner containers on whichever pool member has
headroom — across machines that different people own and administer.

Extracted from `powervaro/homelab` on 2026-08-26 with history preserved. Commits
older than that date were made while this code lived in a private single-operator
repo; read them for rationale, not for current topology.

## The one thing to understand first

**Two components, split exactly along the credential line.**

| Component | Instances | Holds credentials | Responsibility |
|---|---|---|---|
| **Dispatcher** | one per org, in that org's trust domain | yes — that org's GitHub App key | polls its own org's queue, requests capacity, mints the JIT runner token, spawns the lane, releases the reservation |
| **Scheduler** | exactly one, neutral | **no** | host inventory, health, reservation ledger, capacity gates, placement |

The scheduler never contacts GitHub, never sees job contents, and cannot
authenticate as either organisation. Compromising it yields scheduling denial and
job metadata — not the ability to write to either org's repositories.

If a change would give the scheduler a credential, or give a dispatcher authority
over another org's placement, it is wrong regardless of how much simpler it looks.

## Invariants

1. **One scheduler, one ledger, one `events` table.** Admission is globally
   consistent. Distributed admission is a non-goal — see
   [`docs/decisions/0100-one-scheduler-one-ledger.md`](docs/decisions/0100-one-scheduler-one-ledger.md).
2. **A lane host holds no secrets.** The JIT runner token is minted by the
   dispatcher and injected at spawn; short-lived, single-use, never written to
   lane-host disk. (ADRs 0017, 0026.)
3. **Lane containers are never on the CI fabric.** Untrusted job code gets plain
   bridge networking with outbound NAT and nothing else. Anything more is opt-in
   per lane class, per tenant.
4. **No operator's machine is a default.** Any code path that picks a host must
   be given one. A default that names a real machine silently places one
   operator's jobs on another's hardware — see
   `services/ci-controller/tests/test_no_operator_defaults.py`.
5. **Deployment authority is per operator.** Each operator provisions only their
   own machines from this shared code. A commit here reaches nobody's hardware
   until that machine's owner deploys it. Consumers pin this repo **by commit
   SHA, never by branch** — the pin *is* the boundary.

## Layout

- [`services/ci-controller/`](services/ci-controller/) — the dispatcher and the
  scheduler. Same tree today; they are separate processes with separate compose
  services (`main.py` / `scheduler_main.py`).
- [`services/github-actions-runner/`](services/github-actions-runner/) — the lane
  image and its entrypoint.
- [`services/ci-lane-host/`](services/ci-lane-host/) — what a machine runs to
  *offer* capacity: a scoped Docker socket proxy plus a fabric sidecar.
- [`services/github-review/`](services/github-review/) — the PR review bot.
- [`docs/decisions/`](docs/decisions/) — ADRs. **Numbers 0012–0026 were imported
  from homelab and keep their original numbers** so that history, incidents and
  cross-references still resolve. This repo's own series starts at **0100**, which
  is why there is a gap: the two repos can then never collide as both keep
  numbering.
- [`docs/incidents/`](docs/incidents/) — postmortems. Read these before changing
  reaping, admission or health-check behaviour; most of the non-obvious code here
  exists because of one of them.
- [`scripts/`](scripts/) — pool operator tooling. `scripts/pr-land.js` is the
  landing tool (a symlink into the shared submodule, see below).
- [`docs/plans/`](docs/plans/) — `ideas/` → `ready/` → `ongoing/`, one file per
  topic, deleted when verified. Rules in [`docs/plans/README.md`](docs/plans/README.md).
- [`.agents/`](.agents/) — skills (`.claude/skills` is a symlink to
  `.agents/skills`), the `_shared` submodule, and `land.config.json`. Two of the
  skills (`ship-a-feature`, `managing-plans-lifecycle`) are shared with other
  repos and carry procedure only; every value specific to this repo lives here
  and in `land.config.json`. **Run `git submodule update --init` after cloning
  and in every new worktree** — a missing `_shared` is silent: the skills load
  as nothing and `node scripts/pr-land.js` fails with `MODULE_NOT_FOUND`.

## Conventions

- Commit style is `<scope>: <imperative>` — **not** conventional commits. No
  `feat:` / `fix:` prefixes.
- Python 3.12, `uv`, `pytest`, `ruff`, `mypy`. Config is in the root
  `pyproject.toml`; services are the packages.
- Tests are the specification. `services/ci-controller` alone carries ~5.5k LOC
  of them and they encode incident lessons that the code does not restate.
- **This repo is read by both operators.** Nothing private to either belongs in
  it — no personal tailnet addresses, no private service names, no secret values.
  It commits a secrets *schema* only; each operator's real store stays their own.
  That includes `.claude/settings.json`: anything naming a machine, a user or a
  cloud project goes in the gitignored `settings.local.json`.
- **Every change must leave the repo cheaper to navigate and modify next time.**
  Delete rather than deprecate, fix stale docs the moment you see them, rename
  rather than comment-and-leave. **Never silently leave known debt**: something
  you can't fix now is either fixed now or written down as a plan in
  `docs/plans/` — a `# TODO`, a muted test, or a mention in a PR body does not
  count, those vanish. Development time is nearly free here; design quality is
  not. If you are picking an approach because it is less work *now*, that is the
  wrong axis.
- **When dispatching subagents, forbid destructive git in their prompt** —
  `reset`, `commit --amend`, `checkout --`/`restore`/`clean`, `rebase`,
  `add -A`/`add .`, `worktree remove`, and hook-bypass flags, *even in a
  throwaway worktree*. Allow read-only `status`/`diff`/`log`/`show` and have them
  report BLOCKED rather than attempt one. Reaping a worktree is the dispatcher's
  job, and this repo has a submodule, so `git worktree remove` refuses outright —
  verify the worktree is clean and merged, then `rm -rf` it and
  `git worktree prune`; never `--force`.

## Working here — the autonomy contract

**The user is a decider, not a merge gate.** A change should cost them two
messages: their request, and one decision. The shared `ship-a-feature` skill owns
the procedure — read the code first, front-load every business and technical
question into ONE message with your recommended pick on each, take `go` as "all
your picks", then implement and land without further check-ins. For defects,
`fix-bug`; for new behaviour, `tdd`; for a vague proposal, `grill-me`.

**Two work modes — classify from the diff, never ask which:**

- **Direct** — the diff touches *only* `docs/**`, `*.md`, `.agents/**`,
  `.claude/**`. Commit and push straight to `main`. No branch, no PR: these
  paths have no runtime and no lane, and both operators can read what landed in
  `git log`.
- **Autonomous** — everything else. Own `fix/`- or `feat/`-style branch in a
  *separate git worktree* based on `main`, PR to `main`, land with
  `node scripts/pr-land.js`. Never move the main working checkout off `main`.
  Push once per branch — every workflow here sets `cancel-in-progress`, so an
  intermediate push burns a run and cancels its predecessor.

**Landing.** `node scripts/pr-land.js` is a resumable reconciler, not a merge
command: run it, act on the exit code, run it again. `0` merged · `10` CI red ·
`20` review requested changes · `30` **hand to a human** · `40` preflight
failed. Never hand-roll `gh pr merge` — the script carries the skipped-lane
guard and the hard-stop check that a manual merge silently skips. On `10`,
**read the log before touching code**: a starved runner or a flaked download is
not your regression, and inventing a code change to satisfy broken infrastructure
is the worst failure mode of this contract.

**Merge bar: CI green.** Every workflow runs on every PR (no path filters), so
there is no "no CI ran" case. **No reviewer reads the diff yet** — `.agents/land.config.json`
says why, and what flips it. That is a weaker bar than ordago-apps', and the other
operator lives under it too; what keeps it defensible is that nothing on `main`
reaches anyone's hardware until that machine's owner bumps their SHA pin.

**Hard stops — half machinery, half you.** "Green" answers *did the tests
pass*, not *is the blast radius acceptable*.

- **ENFORCED — `pr:land` exits `30` and hands the PR to the user** when a changed
  path matches `hardStop` in `.agents/land.config.json`: the tailnet ACL
  generator, `services/ci-lane-host/**`, the runner image's `Dockerfile` and
  `entrypoint.sh`, `test_no_operator_defaults.py`, and `.github/workflows/**`.
- **NOT ENFORCED — nothing will stop you, so stop yourself and hand the PR
  over:** any change that moves the credential line (invariants 1–3) — a
  credential reaching the scheduler or a lane host, a dispatcher gaining say over
  another org's placement, a lane container joining the fabric — and any change
  to a role interface that a consumer's pin bump would have to react to. None of
  these is a file path; they are the design.

**This repo overrides two `superpowers` skills.** `superpowers:brainstorming`'s
one-question-per-message rule and `superpowers:finishing-a-development-branch`'s
stop-and-ask merge menu are superseded by `ship-a-feature`. Every other
superpowers skill (`systematic-debugging`, `verification-before-completion`, …)
still applies. Brainstorm output lands in `docs/plans/ideas/<topic>.md`, no date
prefix — not under `docs/superpowers/`.

**Be proactive.** End a response with a one-line suggestion when you notice a
manual operation done twice (→ `scripts/`), a workflow worth encoding (→ a
skill), a convention used in three places but undocumented (→ this file), docs
contradicting code (fix or delete — don't work around), or a verified plan still
in `docs/plans/ongoing/` (→ `git rm` it, extracting a decision only if a future
agent would otherwise re-derive it). Surface, then wait — don't pre-implement
large refactors uninvited.

## CI

Runs on **GitHub-hosted runners**, deliberately: this repo's own CI must not
depend on the pool it implements, or a change that breaks the dispatcher takes
out the runners that would have caught it.
