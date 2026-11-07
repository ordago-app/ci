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
`ci-controller` config). Automated review uses no lane: `github-review` polls for new
heads itself.

## Architecture: scheduler + dispatcher, one ledger, multiple hosts

Two containers, split along the credential line (see
[ADR 0025](../../docs/decisions/0025-ci-scheduler-dispatcher-split.md)):

- **`ci-controller`** (this image, default command) — holds the GitHub App key and the
  `docker-socket-proxy` reach. Polls GitHub, mints JIT registration tokens, spawns and
  reaps lane containers.
- **`ci-scheduler`** (same image, `python -m src.scheduler_main`, port 8001) — holds
  **no credentials and no Docker reach**. Owns the reservation ledger, capacity gates,
  host health bookkeeping and placement decisions.

`ci-controller` talks to the scheduler over HTTP (`/plan`, `/lanes`, `/lanes/adopt`,
`/lanes/{id}`). Which transport it uses is one env var: `CI_SCHEDULER_URL` set (as
`compose.yml` sets it here, to `http://ci-scheduler:8001`) selects `HttpScheduler`;
**unset**, `src/main.py` falls back to an in-process `LocalScheduler` and no `ci-scheduler`
container is needed at all — the single-operator, single-container deployment this
service shipped as before the split.

Either way there is **one ledger and one metrics DB**, even when a job ends up scheduled on
another host. `personal/ci-controller.yml`'s
`hosts:` map names each host's own `docker-socket-proxy` endpoint, allowed job classes, and
CPU weight; every host-agnostic knob (RAM budget, lane ceiling, disk budget, RAM floor, load
ceiling, work dirs, mounts, lane env) is inherited from the top-level config unless a host
overrides it. Each tick `ci-controller` health-checks every host — it owns the Docker reach, so host
health is its bookkeeping, not the scheduler's — and a host that stops responding is
skipped for admission and its in-flight lanes are reaped and recorded as `infra_failure`
rather than disappearing silently. It passes the healthy set to the scheduler, which picks,
among the enabled, healthy hosts whose `allowed_classes` admit a job's class, the one with
the most free reservation headroom, ties broken by host name — deterministic, so a replay
of recorded jobs is reproducible.

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
- `repos[].project` — the project slug; its GitHub `owner/name` is resolved from
  `personal/repos.yml`, mounted beside this file. The App that mints registration
  tokens has a different installation id under each owning account, so the owner
  is read from the one place it is written.
- `repos[].label_class` — the allowlist + label→class map (also the ACL).
- `hosts` — per-host `docker_endpoint`, `allowed_classes`, `cpu_shares`, plus optional
  overrides of any of the above (see "Architecture" above and ADR 0016).
- `review_admission` — `cheap_classes` (go first among heads under review),
  `session_host` and `session_ram_mb` (what each running review container is charged).
  Required; see "Review-aware admission" below.

`GITHUB_REVIEW_URL` (required env, `http://github-review:8000` in `compose.yml`) is where
per-head review state is read.

## Review-aware admission

See [ADR 0032](../../docs/decisions/0032-review-aware-admission-binds-through-a-run-pin.md).
Each tick the controller asks github-review's `GET /heads` for the review state of every
queued job's head and stamps it on the job. Admission then ranks jobs **pinned to their
own run** (`runs-on` carries `run-${{ github.run_id }}`): approved or unreviewed first,
then heads under review (cheap classes before heavy), then heads with changes requested,
which are deferred `review_held` while anything ranked above them waits on a capacity
gate. Largest-first orders jobs within a rank. An unpinned job is not ranked, because
GitHub would hand its lane to any job with the same labels.

While another job waits for a lane, rejected heads' runs with no job in progress and no
lane held by the controller are cancelled (`cancel` events in `metrics.db`). Needs the
runner App's Actions: write.

github-review unreachable: jobs admit untiered, largest-first, with an ERROR log and
`review_state_reachable: false` in `/status`. Never a stall.

Review containers (`claude-review-*`, `codex-review-*`) on `session_host` are counted
every tick and charged `session_ram_mb` each against that host's budget, so reviews and
lanes share one capacity model. `/status` reports the count as `review_sessions`; it is
not part of `ledger_ram_mb`, which sums lanes.

## Security model

- The controller holds the runner App private key; **lanes never do** (they get only a
  short-lived registration token). Safe because every onboarded repo is private + single-owner.
  The App's Actions: write is used for one call only, cancelling a rejected head's
  still-queued run (`GitHubAdapter.cancel_queued_run`).
- The controller talks to Docker only through `docker-socket-proxy`, scoped to
  container create/start/list/remove. No raw socket, no `exec`, no images/volumes.
- `/metrics` + `/status` are `expose`-only (no Caddy route, no public ingress).

## Ops

- Health: `docker exec ci-controller python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"`
  (the slim image ships Python, not curl). Same one-liner against `ci-scheduler` on port
  8001 checks the scheduler; both are on `dashboard_health_targets`.
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
  Both are null rather than zero when unknown. `deferred[].review` is the head's review
  state, null when github-review has no row for it.
- Onboard a repo: `make ci-onboard REPO=owner/name`.
- Deploy: from the main checkout, `... services.yml --tags ci-controller --diff`.
