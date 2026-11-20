# A lane's RAM reserve is a budget, not a limit

**Priority:** high

## What was observed

2026-11-20, from about 03:45 UTC. One `node_heavy` lane on powerserver, ordago-apps'
`Emulators · AUTO fidelity (release PR)` job on `release/0.49.0` (run 44657503475), was
admitted against its 8600 MB reserve and grew to **12.15 GiB**. `docker stats` showed its
limit as the host's 15.54 GiB, which means it had no limit at all. The host had 844 MB free
and swap full (2034 of 2047 MB). The load average climbed 21 → 50 → 104 → 131 within
minutes. `tailscale ping` and SSH timed out most of the time.

The host-pressure guard did its job: `HOLDING new lanes (host_pressure)`. But holding new
lanes cannot shrink the one already running. Every other job on the pool waited behind it,
and homelab's jobs had no runner for hours. The 2026-08-07 overcommit and the 2026-08-20
double crash began the same way: the kernel picking an OOM victim on a host that also runs
the operator's other services.

## Why it happens

Admission (`admission.py`) counts each class's `ram_mb` against the host budget. The lane
container (`docker_adapter.py`, the `containers.run` kwargs) is started with no
`mem_limit`, so nothing makes the job stay inside the number it was admitted with. A job
that outgrows its reserve takes its overrun from the host: from the floor, the other lanes,
and the operator's services. ADR 0015 decision 6 already says why that is the worst place
for it: "RAM exhaustion … the kernel OOM-kills something, possibly not the CI job".

## Proposal

Give every lane container a hard memory limit derived from its class's reserve. A runaway
job then gets OOM-killed inside its own cgroup and fails as a red check. The host and the
rest of the pool keep running.

- `mem_limit` = reserve × headroom factor. Admission keeps budgeting with `ram_mb`; the
  limit only bounds the tail.
- `memswap_limit` = `mem_limit`, so a capped lane cannot move its overrun into swap either.
  Here swap was what filled.
- **Record the kill.** The container is `auto_remove`, so `State.OOMKilled` is gone before
  anyone can read it. The controller watches Docker's `oom` / `die` events for lanes and
  writes an `oom_killed` event with class, job and peak. That way a too-small reserve
  shows up in `ci-bench` and `make ci-status`, not as an unexplained job failure.
- The job's owner is told through the failing check. Its log ends abruptly, so the step
  summary or the controller's PR-facing status should say "killed at the lane memory cap
  (N MB)".

## Open decisions

1. **Headroom.** (a) A factor, e.g. 1.25× `ram_mb`. (b) A fixed margin, e.g. +1500 MB.
   (c) A per-class `ram_limit_mb`, defaulting to (a).
   Pick: (c) defaulting to 1.25×. The small classes have about 2% headroom by design
   (`node_small`'s "WATCH: 2700"), so one factor would either kill them on noise or barely
   bind the big ones. Each class's p99/max from `docs/ci-capacity-report.md` sets its value.
2. **Rollout.** (a) All classes at once. (b) Measure-only first: set the limit very high and
   log what each class would have hit. (c) Start with `node_heavy` and `emulator`.
   Pick: (b) for one week, then (a). A cap that kills green jobs costs more trust than one
   night's starvation.
3. **Emulator lanes.** The Android emulator's guest RAM is allocated inside the container.
   Its limit must cover the AVD's configured RAM plus Gradle, or every emulator job dies at
   boot. Decide its value from measured peaks, not the 10400 reserve alone.
4. **The job that triggered this.** ordago-apps' AUTO-fidelity job either fits a measured
   reserve (raise `node_heavy` to its real peak) or boots fewer function runtimes. That is
   ordago-release's call; this plan only makes sure the next overrun fails that one job.

## Not in scope

- CPU limits. CPU stays a relative weight (`cpu_shares`), never a cap. Slow is fine;
  RAM exhaustion is not (ADR 0015 decision 6).
- Changing admission or the host-pressure guard. Both behaved correctly on 2026-11-20.

## Code it touches

- `services/ci-controller/src/config.py`: class `ram_limit_mb`, validated `>= ram_mb`.
- `services/ci-controller/src/docker_adapter.py`: `mem_limit` / `memswap_limit` in
  `run_kwargs`; an OOM event watch.
- The lane image: nothing, if the step summary is written by the controller.
- Tests: config validation, the run kwargs per class, and an OOM event turning into an
  `oom_killed` event.
