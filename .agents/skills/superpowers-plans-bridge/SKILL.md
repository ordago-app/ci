---
name: superpowers-plans-bridge
description: Use when superpowers:brainstorming, superpowers:writing-plans, superpowers:executing-plans, or superpowers:subagent-driven-development runs in a repo that uses the docs/plans lifecycle. Those skills write to docs/superpowers/specs|plans/ with date-prefixed filenames; this redirects their output into docs/plans/ideas/ with the date stripped, so there is one plan index instead of two.
---

# Superpowers ↔ plans lifecycle

Load this only when **both** are in play: the `superpowers` skills and the
`docs/plans/{ideas,ready,ongoing}/` convention from `managing-plans-lifecycle`.

Superpowers owns the *content* — design questions, task breakdowns, execution.
`managing-plans-lifecycle` owns the *location and lifecycle*. They do not otherwise
overlap; this file only resolves where the files land.

## The one conflict

Superpowers writes specs and plans to `docs/superpowers/specs/` and
`docs/superpowers/plans/`, named `YYYY-MM-DD-<topic>-design.md`. The plans lifecycle
wants `docs/plans/ideas/<topic>.md`. Two mismatches — the directory and the date prefix.

**Resolution: the plans lifecycle wins on location, superpowers wins on content.**

| Superpowers skill | Writes | Redirect to |
|---|---|---|
| `brainstorming` | a spec | `docs/plans/ideas/<topic>.md`, date stripped |
| `writing-plans` | a task breakdown | append to the *same* file when promoting `ideas/` → `ready/` |
| `executing-plans` | reads a plan | read from `docs/plans/ongoing/` |
| `subagent-driven-development` | reads a plan | read from `docs/plans/ongoing/` |

No separate spec/plan file split — **one file evolves through the stages**, keeping the
same filename from `ideas/` to `ongoing/`.

## Rules

- **Never create `docs/superpowers/`.** If a superpowers skill produces a file there
  anyway, move it to `docs/plans/ideas/` and strip the date prefix before doing anything
  else. Do not leave it for later.
- **Strip the date on import too.** A plan moved in from another repo drops its date
  prefix during the move.
- **One index.** The point of the redirect is that `ls docs/plans/ongoing/` answers "what
  is in flight" completely. A second location silently defeats it.

## Why the date prefix goes

The filename is stable across the whole lifecycle. A date that was accurate when the
proposal was drafted is misleading by the time implementation starts — and `git log` plus
the `ongoing` Status header already answer every timing question worth asking.
