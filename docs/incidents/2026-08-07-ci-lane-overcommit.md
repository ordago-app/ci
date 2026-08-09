# CI lane overcommit

## Symptom

powerserver became unreachable for ~35 minutes while draining a CI backlog: 15-minute load
average hit 191, `sshd` starved, and four processes were OOM-killed.

## Root cause

`max_concurrent_lanes` had been raised 5→7 (#80). That change looked safe at the time because
a stale `disk_budget_gb.ssd: 6` had been the binding constraint on concurrency, not RAM or CPU —
so fixing the disk budget silently removed the only throttle that had been keeping lane count
in check, and nobody had measured the real CPU/RAM ceiling before raising the lane count.

## Fix

Reverted `max_concurrent_lanes` to 5 (#97).

## What still bites

- Treat `max_concurrent_lanes: 7` as known-unsafe on this hardware (i7-4770, 4c/8t, 16 GB RAM).
  Don't raise it again without a measured CPU/RAM ceiling, not just a cleared disk budget.
- When one admission gate (disk, RAM, CPU) is loosened, check whether it was masking an
  unmeasured limit in another dimension before assuming more concurrency is safe.
