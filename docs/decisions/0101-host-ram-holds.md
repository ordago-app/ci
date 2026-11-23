# 0101 — Host RAM holds live in the scheduler

**Status:** accepted
**Date:** 2026-11-23

## Context

A pool host is often also somebody's workstation or build box. On 2026-11-22 an
operator ran an app release (a Gradle build of about 9 GB plus emulators) on the main
pool host while CI lanes reserved most of its 11 GB budget. The host ran out of memory
three times and the release session was killed.

Nothing in the pool could have prevented it. The ledger only knows about lanes and
review containers. The live `host_pressure` guard gates **new** admissions on
`MemAvailable` at that moment, and when the release started there was still memory
free, so the guard never fired. An operator had no way to say "I need 9 GB here for
the next two hours" before starting.

## Decision

**A hold is a named reservation of RAM on one host, with a TTL, kept by the scheduler
next to the ledger.** Admission charges active holds against their host's budget
exactly as it charges review sessions. So `budget_full` binds, headroom shrinks, and
placement moves work to other hosts. A hold never evicts a lane that is already
running. The holder waits for running lanes to finish (`hold_cli reserve --wait`).

- **In the scheduler, not the dispatcher.** Holds are capacity, and capacity has one
  owner (ADR 0100). A hold placed through any path is seen by every dispatcher's next
  plan.
- **In memory, lost on restart.** Holds are like the ledger, but unlike lanes nothing
  can re-adopt them, so they need an owner who renews them. The TTL (60 s to 12 h) keeps
  a hold from outliving a forgotten owner. Reserving again under the same name renews it.
- **Always names a host.** There is no default host (invariant 4), and an unknown host
  is refused. So is a hold larger than the host's whole ceiling, because it could never
  fit.
- **Charged against the configured ceiling.** Admission uses whatever budget it is
  using that tick, including an elastic host's offer. `GET /holds` reports the
  configured ceiling, since the scheduler never sees offers outside a plan.

## Consequences

- An operator about to load a pool host runs one command first and gets an honest
  answer: "fits", or "the hold is in place, lanes are still draining".
- A scheduler restart silently releases holds. The CLI's wait loop puts a missing hold
  back, but a holder who is not waiting will not notice. This is documented in the
  README and in the CLI help.
- The holds API is reachable by whatever can reach the scheduler, the same as
  `/plan` and `/lanes`. A hold can deny scheduling, but it carries no credential and
  starts no container, so the credential line is unchanged.

## Rejected

**Evicting lanes to make room.** That would kill green jobs and record them as
failures to protect work the pool does not run. Waiting costs minutes.

**Lowering `ram_budget_mb` in config for the duration.** It needs a redeploy each time,
the change outlives the need, and it is invisible in `/status`.
