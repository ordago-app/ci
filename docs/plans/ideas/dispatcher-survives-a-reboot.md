# The dispatcher survives a reboot, and says so when it does not

**Priority:** medium

Moved here on 2026-11-08 from a consumer's power-resilience plan: the hardware half stays
with that operator, the software half is this repo's.

## Context

`ci-controller` runs in the fabric sidecar's network namespace
(`network_mode: "service:ci-fabric"`). Docker does not restart a container when the one
whose namespace it borrows restarts, so a sidecar bounce leaves the dispatcher running with
no DNS: every container reads `Up`, `/status` returns 500, and no lane is spawned. On one
operator's pool this happened on two consecutive reboots (2026-08-28, 2026-08-30): the
sidecar finished starting 33 to 39 s after `ci-controller`, and each time CI was silently
down until someone ran `docker restart ci-controller` by hand.

Already in this repo: `services/ci-controller/compose.yml` gives `ci-controller` a
healthcheck on `/status` (resolution, not liveness), and `roles/ci_controller` installs
`ci-controller-healthcheck.timer`, which restarts it when docker marks it unhealthy (#8).
Two things are still missing.

## Tasks

1. **Prove the recovery on a live dispatcher host**, not in check mode. On the host:

   ```bash
   docker restart ci-fabric
   sleep 90
   docker exec ci-controller python3 -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/status', timeout=5).status)"
   ```

   Expected `200`, unattended. Before the healthcheck and watcher, the same commands left a
   name-resolution failure that never cleared. Then confirm the same after a real reboot of
   the host. Record the result in the operator's own runbook or incident, not here.

2. **Make the silence loud in `scripts/ci_status.py`.** It reads `/status` and prints
   lanes; a dispatcher that cannot reach its scheduler currently shows up as a Python
   traceback from `urlopen` (or, if `/status` ever answers from a cache, as a healthy idle
   pool). Print one clear line instead, such as
   `DISPATCHER CANNOT REACH THE SCHEDULER (/status 500)`, exit non-zero, and add a test for
   it. A 200 from an endpoint is not evidence the thing behind it works.

## Done when

`docker restart ci-fabric` leaves `/status` at 200 within ~90 s on a live pool, and
`ci_status.py` reports the unreachable-scheduler state as a failure, covered by a test.
