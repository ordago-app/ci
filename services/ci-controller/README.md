# ci-controller

Dynamic, capacity-admitted ephemeral GitHub Actions runners for the homelab.

## What it does

Polls each allowlisted repo's Actions queue (outbound, every `POLL_INTERVAL_SECONDS`),
classifies each queued job by its `runs-on` label, and admits jobs against a
**RAM-budget-per-class** ledger. Admitted jobs get a freshly-minted, single-use
registration token and a one-job (`--ephemeral`) runner container spawned via a
**scoped docker-socket-proxy**. When a lane finishes, its container auto-removes and
its RAM reservation frees. Excess jobs stay queued on GitHub (back-pressure).

Supersedes the static pools in `services/github-actions-runner` (which become
`ci-controller` config). CI-green → review is unchanged — handled by the existing
`request-review.yml` reusable workflow.

## Architecture: one controller, multiple hosts

One controller instance runs on `powerserver` and admits against **one ledger and one
metrics DB**, even when a job ends up scheduled on another host. `personal/ci-controller.yml`'s
`hosts:` map names each host's own `docker-socket-proxy` endpoint, allowed job classes, and
CPU weight; every host-agnostic knob (RAM budget, lane ceiling, disk budget, RAM floor, load
ceiling, work dirs, mounts, lane env) is inherited from the top-level config unless a host
overrides it. Each tick the controller health-checks every host; a host that stops responding
is skipped for admission and its in-flight lanes are reaped and recorded as `infra_failure`
rather than disappearing silently. Among the enabled, healthy hosts whose `allowed_classes`
admit a job's class, the controller picks the one with the most free reservation headroom,
ties broken by host name — deterministic, so a replay of recorded jobs is reproducible.

Today's second host, `powervaro-ci`, is the operator's desktop running a dedicated,
ansible-managed WSL distro reachable over the tailnet — **opportunistic capacity, never a
peer**: it is `light`-class only (see ADR 0016) and its absence must degrade admission back to
`powerserver` alone, never fail a job outright. See
[ADR 0016](../../docs/decisions/0016-opportunistic-second-ci-host.md) for why two controllers
were rejected, the host-selection tie-break, the class-affinity sequencing, and the isolation
choice for the second host.

## Config

`personal/ci-controller.yml` (mounted read-only). Key knobs:
- `ram_budget_mb` — the only hard gate. Raise after the RAM upgrade.
- `max_concurrent_lanes` — blunt secondary ceiling against CPU thrash.
- `classes` — per-class `ram_mb`, `needs_kvm`, `needs_android_sdk`, `work_disk`, `group_add`.
- `repos[].label_class` — the allowlist + label→class map (also the ACL).
- `hosts` — per-host `docker_endpoint`, `allowed_classes`, `cpu_shares`, plus optional
  overrides of any of the above (see "Architecture" above and ADR 0016).

## Security model

- The controller holds the runner App private key; **lanes never do** (they get only a
  short-lived registration token). Safe because every onboarded repo is private + single-owner.
- The controller talks to Docker only through `docker-socket-proxy`, scoped to
  container create/start/list/remove. No raw socket, no `exec`, no images/volumes.
- `/metrics` + `/status` are `expose`-only (no Caddy route, no public ingress).

## Ops

- Health: `docker exec ci-controller python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"`
  (the slim image ships Python, not curl).
- Live state: `curl http://ci-controller:8000/status` from another homelab container
  (shows the ledger, running lanes, and **why** queued jobs are deferred). Both lists
  carry the job's `class`, `host`, `workflow` and `job_name`, so a reader can answer
  "which *kind* of job is running/waiting, and where" without re-deriving it — that is
  what the dashboard's per-class breakdown is built from. `deferred[].class` is null
  only for `already_running` and `not_allowlisted`, which have no class to name.
  `running[].running_seconds` is the lane's container age, read from the **daemon**
  each reconcile rather than stamped at spawn, so a controller restart does not reset
  the age of a lane that never stopped. `deferred[].waiting_seconds` is measured from
  the first tick that deferred the job and *is* in-memory, so it does reset on a
  restart — the durable version of that history is the `defer` events in `metrics.db`,
  and querying those behind a page that polls every 5s is not a trade worth making.
  Both are null rather than zero when unknown.
- Onboard a repo: `make ci-onboard REPO=owner/name`.
- Deploy: from the main checkout, `... services.yml --tags ci-controller --diff`.
