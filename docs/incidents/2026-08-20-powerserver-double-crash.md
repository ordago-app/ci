# powerserver double crash, and the lane leak it exposed

Two unrelated findings from one morning. The crash is **not diagnosed** — this records the
evidence and the instruments that were missing. The lane leak **is** diagnosed and fixed.

## 1. The crashes — undiagnosed, instruments now the priority

Three boots on 2026-08-20 (UTC):

| boot | start | end | ending |
| --- | --- | --- | --- |
| -2 | 04:01:05 (scheduled nightly) | 09:16:49 | abrupt |
| -1 | 09:17:29 | 09:31:34 | abrupt |
| 0 | 09:33:09 | — | current |

### The discriminator

`journalctl --list-boots` gaps are **not** diagnostic on this box — the scheduled nightly
reboot also leaves a ~40 s gap. What discriminates is the last line of the boot:

- Clean shutdown ends with `systemd-journald[…]: Journal stopped` (boot -3 at 04:00:21,
  boot -11 at 04:00:25) or `systemd-logind: System is powering down` (boot -6, 08-07).
- Both of today's boots end mid-stream on an ordinary `[UFW BLOCK]` kernel line. No
  shutdown sequence, no panic, no MCE, no thermal event.

By that test the signature has now occurred **three** times: 2026-08-02 11:23:44, and twice
today. It is accelerating.

### What was ruled out, and how

- **Not a load event.** This was the first hypothesis (both endings follow lane spawns by
  seconds, and the 2026-08-07 incident was a load event). `sar -q` from the sysstat
  archive refutes it: `ldavg-1` was **1.11** at 09:10 and **2.09** at 09:30:30 — roughly
  64 s before the second crash — on 8 threads. Nothing was building. This is not a repeat
  of 2026-08-07.
- **Not a docker/runner resurrection.** Runner containers are `restart=no autoremove=true`,
  so nothing came back by itself.
- **Not the net-watchdog.** `homelab-net-watchdog.timer` firing every ~2 min is its normal
  cadence (it ran cleanly at 09:31:15, 19 s before the crash), not link flapping.

### The instruments that were missing

This is the actionable part. The cause is untestable right now because nothing was watching:

- **`lm-sensors` is not installed** (`dpkg -l` shows `un`). `sensors` returning nothing is
  *no thermal data*, not *no thermal event*. The thermal/VRM branch has never been tested.
- **No EDAC.** `/sys/devices/system/edac/mc/` has no `mc*` controllers — consumer board,
  no ECC. Memory errors are invisible.
- **`/sys/fs/pstore/` is empty.** Consistent with power loss rather than a panic, but the
  backend was never verified, so absence here is weak evidence.

Journald also stopped ~17 s before the box actually died: the controller's sqlite events
table has `defer` rows at 09:31:40 and 09:31:51, past the journal's last line at 09:31:34.
Time the crash from the events DB, not the journal.

### Next step

Do **not** swap hardware on this evidence — there is no positive signal to act on.
Instrument first, attribute on the next occurrence.

`lm-sensors` turned out to be the wrong instrument, and `sensors-detect` is not needed at
all: the kernel drivers are already loaded on this box. `coretemp` (i7-4770 die temps),
`x86_pkg_temp`, `acpitz` (board ambient), an `nvme` hwmon, and — the one that matters for
a power hypothesis — `intel_rapl`, which exposes cumulative package energy in µJ. All of
it was readable the whole time. Nobody was reading it and nothing stored it.

Note `sensors-detect` finds nothing under WSL2 regardless of hardware (no `/dev/port`, no
PCI SMBus passthrough), so a "no sensors detected" result there says nothing about this
host. Run it on the box, or better, skip it — see the `hwrec` tasks in
[`ansible/playbooks/base.yml`](../../ansible/playbooks/base.yml), which sample the loaded
drivers directly.

Baselines recorded 2026-08-20 after the recorder went in, for the next reader to compare
against: package **36 °C / ~3.3 W** idle, **62-64 °C / ~40 W** under five busy CI lanes.
An i7-4770 is 84 W TDP and Tjmax 100 °C, so neither temperature nor CPU power is anywhere
near a limit under this box's worst normal load. That is a real (if negative) result: it
makes a *CPU* thermal or power-draw cause unlikely, and moves suspicion toward the PSU
rails, mains, or the board.

Still open: whether the MS-7848 exposes an ACPI ERST / `ramoops` pstore backend. And the
only instrument that can actually distinguish mains loss from a board fault is a UPS with
USB reporting (`nut`) — a hardware purchase, not a config change.

## 2. The lane leak — diagnosed and fixed

### Symptom

`make ci-status` at ~09:54 showed a `node_heavy` lane `booting` for 1138 s while holding
its full 7500 MB reservation, with `deferred: {budget_full: 7}`.

### What actually happened

The events DB (`/var/lib/ci-controller/metrics.db`) has the whole story:

- lane `powerserver-cici-96380840900-658a75` — `admit` at **09:35:35**, no `attach` ever,
  `reap` at **10:11:28** with conclusion `unattributed`.
- It held 7500 MB for **2153 s**, 3.6x the `idle_lane_max_seconds: 600` "absolute ceiling".
- The exit was a `reap` (reconcile noticing the container was gone), **not** an
  `idle_reap`. The idle reaper never tore it down. The container died on its own.
- The lane was provably jobless the whole time: its job 96380840900
  (`Emulators · Vitest (functions) + E2E (shared)`) was still `status: queued` with
  `runner_name: ""` when queried at 10:20 — 45 minutes after being queued at 09:34:21.

It also did **not** "clear on its own by 09:55" as first thought. The two `idle_reap`
events at 09:49:37 and 09:50:16 are unrelated `light` lanes.

### Root cause

`idle_lane_max_seconds` bounds when a reap is **attempted**, not how long a lane can hold
its reserve. Every bail-out in `_reap_idle_lane` returns and retries next tick with no
bound and no event:

| path | condition |
| --- | --- |
| `deregistration_refused` | GitHub 422s the DELETE (calls the runner busy) |
| `deregister_error` | the deregistration call raised |
| `absence_unconfirmed` | unregistered lane, no positive absence signal this tick |
| `container_remove_failed` | the docker remove raised |

Each guard is individually correct — none of them is evidence the lane is safe to destroy,
and inferring "gone" from a refusal is exactly what would kill a live job. The defect is
that they are unbounded *and silent*: `status()` reported
`"state": "busy" if running_job_id is not None else "booting"`, so a lane 3.6x past the cap
was indistinguishable from one that started 20 s ago.

Which of the four paths fired here is **not** determined — the container's stdout log for
09:35–10:11 had rotated by the time it was searched. That gap is itself the finding.

### Fix

Instrument, do not force. The refusals stay; what changes is that they are now visible.

- `Reservation` carries `reap_blocked_reason` / `reap_block_count`.
- Each bail-out calls `_block_reap()`, which records the reason, logs it with the lane's
  RAM and idle time, and emits an `idle_reap_blocked` event (first occurrence, then every
  20th) so `ci_bench` can count it.
- `status()` gained a third state: `busy` / `stuck` / `booting`. `stuck` is any
  unattributed lane the reaper declined to remove, or one past the idle cap.
- `make ci-status` prints stuck lanes per-lane with reason and streak, not aggregated.

No force-removal threshold was added. Picking one would be tuning on inference, and the
failure mode it risks — destroying a lane that is genuinely running an unattributed job —
is worse than the leak.

## What still bites

- **"booting" in `make ci-status` was a lie by omission**, and it cost ~40 minutes of
  forensics. When a lane looks stuck, read the events DB first — it is the only record
  that survives both a container restart and log rotation:

      docker exec ci-controller python -c "import sqlite3,datetime; ..."   # see §2

- The controller's ledger is **in-memory**. The ~5 lanes in flight at 09:16:49 and 09:31:34
  left no `reap` rows at all — the `INFRA_FAILURE` / `LANE_LOST_UNATTRIBUTED` sentinels
  only cover a *remote* host vanishing, not the controller's own host dying. Post-crash,
  those jobs are invisible to `ci_bench`'s infra-failure counts.
- Four jobs in ordago-apps run 32352441887 show `failure` with zero failing steps. Three
  light jobs admitted 09:16:15–09:16:21 died at 09:26:2x — exactly 10:00 after start,
  GitHub's runner-silence timeout — because the host vanished at 09:16:49. These are infra
  failures wearing a test-failure costume; GitHub's UI has no way to say so. Re-run them.
- `powervaro-ci` showing `UNHEALTHY (skipped for admission)` is **not** a fault. The VM's
  power state is the opt-in switch (ADR 0017) — stopped means UNHEALTHY by design. Start it
  with `make ci-lane-up` from the Windows host. Note it allows `[light, node]` only:
  `node_heavy` reserves 7500 against its 6000 MB budget and would defer forever, so it does
  nothing for an emulator backlog.
