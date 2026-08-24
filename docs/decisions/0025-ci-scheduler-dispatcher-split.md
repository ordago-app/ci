# 25. Split `ci-controller` into a scheduler and a dispatcher along the credential line

Date: 2026-08-24

## Status

Accepted.

## Context

A cofounder is becoming a co-operator of the CI platform on equal footing: he will
deploy, hold secrets, and operate CI for his own organization, rather than donating a
machine Álvaro administers. `docs/plans/ideas/federated-ci-pool.md` designs the
resulting system — one scheduler, two dispatchers, a dedicated CI tailnet fabric — but
none of that can start from today's `ci-controller`, because today's `ci-controller` is
one process that both **holds the GitHub App key** and **owns the reservation ledger**.
Any deployment of it shared between two organisations is a shared credential store: a
compromise of the process that decides "does org A get a lane on this host" is also a
compromise of "mint a token that writes to org A's private repos" and, in a pooled
deployment, org B's too.

[ADR 0016](0016-opportunistic-second-ci-host.md) already established the invariant this
decision has to preserve: **one controller, one ledger, one `events` table** — never a
second independent admission decision-maker, because two ledgers can both admit the same
queued job and `ci-bench`'s report is a single join over one table. That invariant
predates the credential question and is orthogonal to it; this ADR does not touch it.

This is Stage 1 of the federated-pool migration (see the spec's migration table): the
refactor happens inside homelab first, with the existing pytest harness, before anything
moves to a shared repository or a second organisation exists to deploy against. It ships
with **no behaviour change** for the single-operator deployment — the scheduler and the
dispatcher (still named `ci-controller`) run as two containers in the same compose stack
on `powerserver`, talking over the `homelab` Docker network, which is exactly where they
were already colocated as one process.

## Decision

**The credential line is the split line**, not any other boundary (not per-host, not
per-repo, not per-class):

| Component | Holds | Responsibility |
|---|---|---|
| **Scheduler** (`ci-scheduler`, port 8001) | nothing — no GitHub App key, no Docker socket reach | reservation ledger, capacity gates, placement decisions |
| **Dispatcher** (`ci-controller`, port 8000, unchanged image and name) | the GitHub App key, `docker-socket-proxy` reach | polls GitHub's job queue, mints JIT registration tokens, spawns/reaps lane containers, calls the scheduler for admission, host health bookkeeping (`_healthy`/`_unhealthy_ticks`), the metrics DB, `/status` and `/metrics` |

Compromising the scheduler yields scheduling denial and job metadata — never the
ability to authenticate as an org or write to a repository, because it has no path to
either. This is the property the federated design needs before a second dispatcher can
exist at all.

The wire boundary is a small HTTP API (`POST /plan`, `POST /lanes`, `POST /lanes/adopt`,
`PATCH /lanes/{id}`, `DELETE /lanes/{id}`, `GET /lanes` — `src/scheduler_api.py`) rather
than a shared library import, so a future off-box scheduler is a config change (point
`CI_SCHEDULER_URL` elsewhere) and not a rewrite. Host health is deliberately NOT on it:
the dispatcher probes the hosts it can reach and passes the healthy set into `/plan`.

**The single-operator deployment keeps working with zero config**: with `CI_SCHEDULER_URL`
unset, `src/main.py` constructs an in-process `LocalScheduler` directly — the same object
`ci-scheduler` wraps in `create_scheduler_app`. Setting
`CI_SCHEDULER_URL` (as `services/ci-controller/compose.yml` now does, pointing at
`http://ci-scheduler:8001`) switches the dispatcher to `HttpScheduler`, which calls the
same API. One code path, two transports, chosen by whether the env var is
present — this is why Task 7's diff added no new admission logic, only a client that
mirrors `LocalScheduler`'s interface.

**One scheduler, one ledger — ADR 0016's invariant is preserved verbatim.** The split
adds a process boundary, not a second admission decision-maker: exactly one
`ci-scheduler` container runs, `LocalScheduler` still does the placement math, and
`ci-bench`'s single-join measurement model reads the same `metrics.db`. That DB stayed
on the **dispatcher**: `ci-controller` mounts `/opt/personal/state/ci-controller` and
points `CI_CONTROLLER_DB` at it, while `ci-scheduler` has no state volume at all — the
events it would want to record (admit, defer, reap) are all written by the dispatcher,
which is also where `ci-bench` runs. This contradicts gap 4 of
`docs/plans/ready/ci-controller-scheduler-split.md`, which predicted the DB would follow
the scheduler; it did not, and nothing had to move.

*Rejected: split along class or repo instead of credential.* Would not change who holds
the App key, so it does not buy the property the federated design needs — a shared
deployment would still be a shared credential store, just with an extra process boundary
that protects nothing.

*Rejected: do the split and the cross-repo move in one step.* The spec's own migration
table already sequences this as Stage 1 (refactor inside homelab) before Stage 2 (move to
a shared repo). `Controller.reconcile()` mixed GitHub polling, ledger mutation, and
container reaping in one loop — a real refactor of code with an existing pytest harness,
not a file move — and that refactor is safer done where the tests already run and where
nothing is blocked on the org conversion (spec Stage 0) landing first.

## Consequences

- **A scheduler outage now stalls admission everywhere** — a new failure mode that did
  not exist when admission lived in the same process as the GitHub poller. Previously a
  crash was one process down, full stop; now a crashed `ci-scheduler` leaves
  `ci-controller` running but unable to admit any job on any host, because `HttpScheduler`
  has no fallback path. This is treated as acceptable in the single-instance deployment
  (`restart: unless-stopped`, same recovery story as today) and becomes the sharper
  operational concern the federated design has to answer once two dispatchers depend on
  the same scheduler.
- **The HTTP wire format is a new compatibility surface.** `scheduler_models.py`'s
  request/response shapes (`PlanRequest`, `AdoptRequest`, `UpdateRequest`, and the
  `decision_to_wire`/`reservation_to_wire` (de)serializers) did not exist before this
  split and now have to change in lockstep across `HttpScheduler` and
  `create_scheduler_app` — a class of bug (skew between what the dispatcher sends and
  what the scheduler expects) that a single in-process call could never produce.
- **The ledger remains in-memory** (`Ledger()`, constructed fresh in both
  `scheduler_main.py` and `main.py`'s fallback path). A scheduler restart now blanks
  reservation state for *every* dispatcher that depends on it, rather than for the one
  controller process that used to own it — the same class of consequence ADR 0017 noted
  for the lane host (`_readopt` rebuilds from live containers, so this is recoverable, not
  silent data loss, but the blast radius grew from "this org" to "every org sharing this
  scheduler"). The rebuild only covers hosts that answer the tick: reservations on a host
  that is unreachable when the scheduler restarts are lost for good, and that host is
  over-admitted once it answers again.
- **Health monitoring gained a target.** `ci-scheduler` serves its own `/healthz` on 8001;
  `inventory/group_vars/all.yml`'s `dashboard_health_targets` now probes it alongside
  `ci-controller`, gated on the same `ci-controller` service flag (no new
  `services-enabled.yml` key — it is one service flag standing up two containers, per
  `services/ci-controller/compose.yml`).
- **What this does not do**: it does not move any code to a shared repository, does not
  stand up a CI fabric tailnet, does not add a second dispatcher, and does not resolve
  spec open question 5 (who health-checks hosts once the scheduler is off-box). All of
  that is later migration stages; this ADR covers only Stage 1.

## Related

- [ADR 0016 — `powervaro-ci`: an opportunistic second CI host under one controller](0016-opportunistic-second-ci-host.md) —
  the one-controller-one-ledger invariant this split preserves.
- [ADR 0017 — the CI lane host is an opt-in VM, not a WSL distro](0017-ci-lane-host-as-an-opt-in-vm.md) —
  the isolation precedent (credential-free lane hosts) this split extends to the
  scheduler.
- [`docs/plans/ideas/federated-ci-pool.md`](../plans/ideas/federated-ci-pool.md) — the
  full federated design; decision 1 and migration Stage 1 are what this ADR records
  having been built.
- [`docs/plans/ready/ci-controller-scheduler-split.md`](../plans/ready/ci-controller-scheduler-split.md) —
  the implementation plan for this split, including the known gaps this ADR does not
  close.
