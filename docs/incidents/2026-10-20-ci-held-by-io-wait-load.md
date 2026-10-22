# CI held by IO-wait load

## Symptom

For about five hours on one pool host, almost every job deferred `host_pressure`,
often while no lane was running. Light jobs waited 1.5–3 h.

## Root cause

The `host_pressure` gate deferred whenever `load_1m > host_load_ceiling`. Load average
counts tasks blocked in uninterruptible IO as well as runnable ones. A co-located,
non-CI container thrashed against its own memory limit and swapped onto the
host's spinning root disk. load_1m sat at 20–50 on 8 threads while the CPUs were
5–8 % busy and most host RAM was available. PSI cpu `some` was below 1 %.

The load ceiling exists to stop CPU oversubscription
([2026-08-07](2026-08-07-ci-lane-overcommit.md)). There was none, so every one of
those defers was a false positive. The reservation ledger was not involved: lane
occupancy rebuilt from `metrics.db` admit/reap events matched it throughout.

## Fix

Load counts as host pressure only while the CPUs are contended: PSI
`/proc/pressure/cpu` `some avg60` ≥ 50 % (`admission._cpu_oversubscribed`). With no
PSI reading the gate falls back to plain load average. The ceiling, the RAM floor,
budgets and lane caps are unchanged.

## What still bites

- IO pressure on the host's root disk still slows dockerd. Container create/start
  calls can exceed a 60 s client timeout. github-review hit exactly that the same
  night (see its fix in `services/github-review/src/providers/codex.py`).
- A pool host whose docker root shares a spinning disk with other workloads gets
  lane boots that are only as fast as that disk.
