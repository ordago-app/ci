# 23. A remote lane host reclaims its own workspaces

Date: 2026-08-10

## Status

Accepted. Refines ADR 0016 (lane host as a dumb docker host) and ADR 0017 (opt-in VM).

## Context

`powervaro-ci` filled its 40 GB disk on 2026-08-09, ENOSPC'd every job, lost networking,
and wedged `multipassd` badly enough to need an elevated `Stop-Process` and a full
recreate.

The immediate cause was a prune that required a work dir to be a day old (`find -mtime
+1`) while a burst created them far faster. Replacing age with liveness fixed that, and a
16 GB per-host `disk_budget_gb` was added on the theory that the disk gate had been unable
to fire. **Measurement showed the gate was not the mechanism, and could not be.**

`admission.py` gates on `ledger.disk_gb_in_use()` — the sum of `work_gb` over lanes
*currently leased*. That models **concurrency**. The disk is consumed by **throughput**:

```
2026-08-09 23:45Z   82 admits in one 15-minute window, through 8 lane slots
                    mypy 38 · pytest 22 · ruff 11 · gitleaks 5 · actionlint 4 · locks 2
```

The gate never saw more than 8 leases and was satisfied every time, while 82 workspaces
landed on the disk. The 23:00Z hour took 202 admits; at the ~200 MB a light workspace
actually measures, that is ≈40 GB — the size of the disk. Setting the budget to
`8 lanes x 2 GB = 16 GB` also made it bind at exactly the point `lane_ceiling` already
did, so it could never fire first regardless.

The deeper cause is that **a lane's workspace outlived the lane's container**. The adapter
binds the work-dir *base* rather than a per-lane subdir, because a bind to a missing path
is created root-owned and locks the uid-1000 runner out of its own workspace. The
workspace is therefore a host directory owned by nobody in the container lifecycle, and
three separate mechanisms grew to clean it up:

1. the entrypoint's `trap cleanup EXIT INT TERM`,
2. `Controller._reap_work_dir`, and
3. this host's `ci-lane-prune-workdirs` systemd timer.

All three can miss. A trap runs on none of SIGKILL, an OOM kill, or a dockerd crash.
`_reap_work_dir` is called only when `res.host == self._host` — not a bug but a fact:
the process cannot unlink paths on another machine. That leaves the timer as the sole
cleanup on a remote host, unmonitored, with no `OnFailure=`, while `healthy()` checks only
that docker answers a ping. A silent timer failure is invisible until ENOSPC.

## Decision

`HostConfig.work_dir_mode` selects who owns a lane's workspace, and remote hosts use
`volume`.

**`bind`** (default, powerserver) binds `work_dirs[class.work_disk]` as today. It is the
only mode that can place a workspace on a *chosen* filesystem, which powerserver needs:
`work_disk` routes ssd to `/mnt/ci-ssd` (NVMe) while the docker root is on the 7200rpm
LVM, and pnpm only hardlinks out of its store when store and workspace share a
filesystem.

**`volume`** (powervaro-ci) gives the lane an **anonymous** docker volume, which dockerd
deletes together with the container `auto_remove` already removes. Cleanup therefore
survives SIGKILL, an OOM kill and a dockerd crash, and needs no filesystem access from the
controller — which is precisely what a remote host cannot grant it.

Three facts make this work, each verified on the host rather than assumed:

- **A volume inherits the image path's ownership; a bind to a missing path does not.**
  Mounting at `/runner-work`, which the runner image pre-creates `install -d -o 1000`,
  yields a uid-1000-owned writable workspace. This is the exact constraint that forced
  the bind-the-base workaround, and it simply does not apply to volumes.
- **The socket proxy permits it.** `VOLUMES: 0` gates the `/volumes/*` paths; an anonymous
  volume rides in the `POST /containers/create` body. Confirmed against the live proxy:
  201 create, 204 start, exit 0, container and volume both gone afterwards.
- **It must be anonymous.** A *named* volume outlives its lane and would need an explicit
  delete through the denied `VOLUMES` capability — leaking exactly like the bind mount it
  replaces, only less visibly.

The mode is chosen per host along the axis where the problem exists — whether the
controller can see that filesystem — not by hostname. `test_deploy_wiring` reads
`RUNNER_HOST` from the deployed compose and fails if any *other* configured host is left
in `bind`.

## Consequences

The disk gate becomes **truthful** rather than decorative. Once a lane's bytes leave with
the lane, live leases *are* the disk in use, which is what `disk_gb_in_use()` computes. The
16 GB ceiling now bounds something real. `work_gb: 2` is kept against a measured ~200 MB
because `work_gb` is an int and `1` would still be 5x reality; the headroom is free while
`lane_ceiling` binds first.

`prune-lanes.sh` is demoted from sole cleanup to backstop, and gains `docker volume prune`
for volumes orphaned by a daemon crash that never ran `auto_remove`. Deliberately without
`-a`: bare `volume prune` removes only *anonymous* volumes — exactly the lane set — while
`-a` would also take unused named ones, the same class of mistake as `image prune -a`
eating the un-restorable runner image. Because nothing routine depends on the timer any
more, its lack of failure alerting stops being load-bearing, and the circuit breaker
considered for that gap (stop the socket proxy on low disk, so the host fails its own
health check) was **rejected**: it is a fourth compensating mechanism for a leak that no
longer exists, it cannot fire until the disk is nearly full, and it fails in-flight lanes.

`volume` cannot honour `work_disk`, since every volume lands under the docker root. That
is why it is not the global default and must not be copied to powerserver: it would move
lanes off the NVMe onto the 7200rpm disk and break pnpm's hardlinks. The two modes are not
ranked — each is correct where placement does or does not matter.

The job-selection change of ADR 0022 attacks the same burst from the other end: 60 of
those 82 admits were `mypy` and `pytest` fan-out, so the identical window today is roughly
28 lanes. Capacity and retention were independent defects and both are now addressed.

The remaining known gap: nothing yet alerts on lane-host disk, and the controller
structurally cannot see it — the proxy denies `SYSTEM` and `INFO` by design, so
`docker system df` is unavailable to it. That is acceptable only because no mechanism now
depends on being told; if a future host reintroduces one, it needs a channel that does not
widen the unauthenticated 2375 surface.
