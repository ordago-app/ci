# 17. The CI lane host is an opt-in VM, not a WSL distro

Date: 2026-08-09

## Status

Accepted. Supersedes [ADR 0016](0016-opportunistic-second-ci-host.md) decisions 6
and 7 (the WSL distro and its firewall caveat), and the `light`-only rationale in
decision 4. Everything else in 0016 — one controller, one ledger, headroom-based
selection, `infra_failure` attribution — stands unchanged and is what made this
change cheap.

## Context

ADR 0016 shipped `powervaro-ci` as a dedicated WSL distro on the operator's
desktop. It worked: 14 real CI jobs completed on it and one `infra_failure` was
correctly recorded when the desktop slept. The controller half of that design is
sound. The **skin** — how the lane host is created, kept alive and reached — was
not, for three reasons that only running it could reveal.

**1. The Windows layer was disproportionately fragile.** Roughly 300 lines of
PowerShell (`wsl-ci-distro.ps1`, `wsl-ci-sleep-inhibit.ps1`) produced four
separate defects in a single day:

| Defect | PR |
|---|---|
| WSL terminates a distro when its last client process exits; nothing started it at logon | #115 |
| `Register-ScheduledTask` needs elevation; failed with a raw CIM error | #118 |
| The logon task pointed at `\\wsl.localhost\…` — a share served by the subsystem it exists to start | #119 |
| `[uint32]0x80000000` throws in PowerShell 5.1 (a hex literal is typed Int32), killing the agent on every launch | #123 |

The last one masked the middle two behind an identical `LastTaskResult=1`. The
agent **never once ran**; every time the host stayed up, it was a manual
`Start-Process`. Each defect was found by the operator pasting an error, because
CI is Linux and can execute none of this. A component that cannot be tested by
the system it belongs to will be debugged by a human, one round trip at a time.

**2. The isolation was weaker than believed, and then it broke.**
`networkingMode=mirrored` put every distro in Windows' network namespace, so no
address was private to the lane host — not even `127.0.0.1`. On 2026-08-09 that
same mirrored mode was found to be breaking **all** loopback TCP on the desktop
(VS Code Remote hung in `SYN-SENT`) and was disabled. Under NAT the lane host is
unreachable at `powervaro…:2222`, so the shipped design was already broken by a
change made for unrelated reasons — which is itself the finding: the lane host
had no boundary of its own to defend.

**3. The operator's requirement changed once they lived with it.** Stated
directly: *the runner must not interfere with the machine I program on.* The WSL
design shared a kernel, an elastic RAM pool and an unbounded 1 TB max VHDX with
the dev distro; the disk half of that contributed to `C:` reaching 98% full and a
`Bus error` during an image build.

## Decision

1. **`powervaro-ci` is a Multipass VM**, reusing the Hyper-V + Tailscale pattern
   this repo already runs for `homelab-vm`. It gets its own kernel, its own
   network stack, and its own tailnet node — so it is reached at
   `powervaro-ci.<tailnet>` on **port 22**. The 2222 that ADR 0016 needed existed
   only to tell the distro apart from Windows inside a shared namespace; a
   non-default port here now would mean that workaround came back.

2. **Opt-in: off unless explicitly lent.** `make ci-lane-up` / `make
   ci-lane-down`. This requires **no controller code and no config change**: a
   stopped VM fails the health check, is skipped for admission, and shows
   `UNHEALTHY` in `make ci-status` — behaviour that already existed and was
   already tested. `enabled: true` stays true permanently; the VM's power state
   is the switch.

   This is what invalidates ADR 0016 decision 6's rejection of a VM. That
   rejection priced a **static RAM carve-out on an always-on 32 GB box**. A VM
   that is off by default costs only disk.

3. **The envelope is a set of hard caps, not targets**: 4 CPU / 6 GB / 40 GB. The
   disk cap is the load-bearing one — it makes the 98%-full incident structurally
   impossible rather than something to remember.

   **The 6 GB has to be taken from WSL, not found.** The first `multipass launch`
   failed outright — `Not enough memory in the system` — with `.wslconfig` set to
   `memory=22GB` on a 31 GB box: WSL's ceiling plus the lane VM plus Windows and
   the operator's apps came to ~36 GB. WSL's budget was lowered to 16 GB on
   2026-08-09 to make room. This is the cost the ADR 0016 analysis missed by
   reasoning about a *shared elastic pool*: a Hyper-V VM needs its RAM committed up
   front, so a second fixed allocation on the same box is a real subtraction from
   the machine the operator programs on, not spare capacity. It is affordable here
   because WSL was never using its full 22 GB — but "give the VM 6 GB" was not free,
   and a future envelope increase is not free either.

4. **The Windows layer is deleted, not ported.** Both `.ps1` files, the
   `homelab-ci-sleep-inhibit` scheduled task, and every WSL branch in the play and
   docs. The sleep-inhibit's problem *disappears* under opt-in: it existed because
   a sleeping desktop killed lanes, and the answer now is that the operator lends
   the machine when not using it. If it ever proves necessary it returns as a
   plain scheduled task with no WSL coupling — a far smaller thing.

5. **A lane host runs a different image: `:light`, built with `WITH_ANDROID=0`.**
   Added 2026-08-09 after provisioning. The Android SDK layer is ~10 GB that a
   `light`-only host can never use, and downloading it is the least reliable part
   of the build: `sdkmanager` blocks in a socket read with no timeout, which wedged
   the provision twice with no output, no CPU and no bytes — indistinguishable from
   a dead network until a thread dump showed `SocketDispatcher.read0`.

   This is the "slim runner image" ADR 0016 and the migration plan both deferred,
   on the reasoning that a large image "cannot hurt the host" inside a capped disk.
   That was true about disk and wrong about provisioning: it did not hurt the host,
   it prevented there being one.

   `runner_image` therefore becomes per-host, inheriting from the top level exactly
   as `work_dirs` does, so powerserver is untouched. Two invariants are pinned by
   tests, because the socket proxy denies `IMAGES` and `BUILD` and the controller
   can only run an image that is already there: the tag ansible builds must equal
   the tag the config names, and a host built without the SDK must not allow a class
   that needs it.

6. **`allowed_classes` stays `[light]`, for a new reason.** Decision 4 of ADR 0016
   gated `node` on the sleep-inhibit shipping. That gate is gone with the script,
   and what replaces it is a promise rather than a mechanism. Widening therefore
   waits on `make ci-report` data about real `infra_failure` rates on this host.

### Rejected alternatives

- **Keep mirrored networking and add a Hyper-V firewall rule.** Would have
  restored reachability, but mirrored mode was disabled because it broke the
  operator's actual work. Re-enabling it to serve CI inverts the priority the
  whole change exists to honour.

- **Harden the WSL distro in place** (fix the fourth PowerShell defect, keep
  going). Rejected because the defect rate was the signal, not the defects. Four
  in one day in code no test can execute, on a foundation that still shared a
  kernel and an unbounded disk with the machine it was supposed to leave alone.

- **Give up on the second host.** Rejected: the controller work is done and
  proven, and the marginal cost of a VM is one `make` target.

## Consequences

- **Capacity is now discontinuous.** The pool is 5 lanes normally and 7 when lent.
  `make ci-report` will show what the host actually contributed; if that is
  negligible, the honest conclusion is that opt-in capacity is not worth having
  and ADR 0012's "replace `powerserver`" is the real answer.

- **Genuinely isolated now:** kernel, network namespace, RAM, disk. The
  `DOCKER-USER` restriction means what it says — ADR 0016 decision 7 had to
  caveat that it could not cover callers in the host's own namespace, and that
  caveat is gone. **Not isolated:** the Windows host itself; a hypervisor escape
  reaches the operator's desktop. Accepted, as before, for private single-owner
  repos with no fork PRs.

- **Reclaiming the machine mid-job costs a lane.** `make ci-lane-down` reaps live
  lanes as `infra_failure`. This is visible rather than hidden, which is the
  property ADR 0016 decision 8 was built for, but it means the operator should
  reclaim when idle.

- **The one-time provisioning is longer** (a VM launch and an image build, versus
  importing a rootfs), and the disk cost is a 40 GB cap rather than an
  unpredictable VHDX. Both are paid once, visibly, instead of continuously and
  invisibly.

- **The lane-host play got simpler**, which is the durable win: `tailscale ip -4`
  replaces a CGNAT regex over the host's interfaces, because the host now runs
  the daemon that owns its address.
