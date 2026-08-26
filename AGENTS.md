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
- [`scripts/`](scripts/) — pool operator tooling.

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

## CI

Runs on **GitHub-hosted runners**, deliberately: this repo's own CI must not
depend on the pool it implements, or a change that breaks the dispatcher takes
out the runners that would have caught it.
