# 0100 — One scheduler, one ledger

**Status:** accepted
**Date:** 2026-08-26

## Context

This repo's founding invariant was established in `powervaro/homelab` ADR 0016,
"opportunistic second CI host". That ADR is **not** part of this repo and will not
be: alongside the invariant it documents one operator's WSL
`networkingMode=mirrored` desktop topology, down to a personal tailnet address.
An ADR is a historical record of a decision and the circumstances around it.
Redacting one to make it shareable produces a document that misrepresents what
was decided and why — worse than not having it. So the decision is restated here,
for a pool that now spans two operators, and the original stays where it was made.

The pool admits jobs from more than one organisation onto machines owned by more
than one person. Something has to decide, globally, whether there is room.

## Decision

**Exactly one scheduler, holding exactly one ledger and one `events` table.**

Admission is globally consistent. Host selection is headroom-based with a
deterministic name-ascending tie-break. Every reservation, every release and every
deferral is one row in one table, whichever dispatcher caused it.

Dispatchers are per organisation and hold credentials. The scheduler is singular
and holds none. Capacity is a shared, contended resource; credentials are not.
That is the whole shape of the split.

## Consequences

- The scheduler is a single point of failure for *admission*. Running lanes are
  unaffected by its absence; new ones are not scheduled. This is accepted: a pool
  that cannot agree on capacity double-books hardware, and double-booked hardware
  is how a host runs out of RAM mid-job.
- The ledger holds both organisations' repo names, timings and outcomes, readable
  by both operators. Accepted between cofounders. **Revisit if a third
  participant appears** — that is the trigger, not a vague "later".
- Distributed admission is a **non-goal**. Two independent schedulers with a lease
  protocol would preserve credential separation and pooling, and would replace a
  solved problem with the hardest one in the design. It also breaks `ci-bench`'s
  single-join measurement model.

## Rejected

**One controller holding both orgs' App keys.** Simpler, and makes a single
compromise a two-organisation repository-write incident.

**A scheduler that health-checks hosts by dialling their socket proxies.** It
would need socket-proxy reach on every machine in the pool, which would let a
credential-free component spawn containers pool-wide — defeating the entire split.
How hosts get health-checked once the scheduler moves off-box is still open; it is
irrelevant while it shares a compose stack with a dispatcher.
