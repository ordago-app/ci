# ci

Federated CI platform shared by two operators. One pool, one ledger, two
organisations, machines owned and administered separately.

- **Dispatcher** — per org, holds that org's GitHub App key, spawns lanes.
- **Scheduler** — exactly one, credential-free, owns placement and the ledger.
- **Lane host** — any machine offering capacity: scoped Docker socket proxy plus
  a CI-fabric sidecar. Holds no secrets.

Start at [`AGENTS.md`](AGENTS.md), then
[`docs/decisions/0100-one-scheduler-one-ledger.md`](docs/decisions/0100-one-scheduler-one-ledger.md).

Consumers pin this repo **by commit SHA**. That pin is what keeps deployment
authority with each machine's owner.

## Using the roles

This collection ships three roles. Every play using any of them must set
`become: true` — the source plays had it, the roles assume it, and `become`
cannot be asserted from inside a role.

- **`ordago.ci.ci_controller`** — the dispatcher (controller, scheduler,
  fabric sidecar). Requires:
  - `services_root`, `personal_root`, `operator_user` — from your inventory.
  - `ci_controller_pool_config` — path to your own `ci-controller.yml`. No
    default; this collection ships no pool config for you.
  - `ci_controller_repos_config` — path to your own `repos.yml`. Not placed
    by this role (it's rendered outside the ported range); it must already
    exist on the target host before the role runs. Defaults to
    `{{ personal_root }}/repos.yml`.
  - `ci_controller_work_dirs` — list of lane work-dir bases on this machine. No
    default: a disk layout is the one thing that cannot have a sensible
    cross-operator value. Must match the `work_dirs` of this host's entry in
    your pool config — the dispatcher bind-mounts those paths directly.
  - `ci_controller_cache_root` — parent for the shared pnpm/gradle/android-sdk
    caches. No default, and it must sit on the **same filesystem** as the work
    dirs that use it: pnpm hardlinks from its store into `node_modules`, and a
    cross-filesystem store silently degrades to full copies.
  - `ci_controller_secrets_dir` — where your secrets `env_file`s were
    rendered. Defaults to `{{ personal_root }}/secrets`, but
    `services/ci-controller/compose.yml` currently hardcodes
    `/opt/personal/secrets/...` — relocating this for real requires
    templating that compose file, which this collection does not do yet.

- **`ordago.ci.github_review`** — the agentic PR review bot. Requires:
  - `services_root`, `personal_root`, `operator_user`.
  - `github_review_config` — path to your own `agent-review.yml`. No
    default, same reasoning as `ci_controller_pool_config`.
  - `github_review_repos_config` — path to your own `repos.yml`, same
    "must already exist" contract as `ci_controller_repos_config`. Defaults
    to `{{ personal_root }}/repos.yml`.

- **`ordago.ci.ci_lane_host`** — offers CI capacity from any machine (scoped
  Docker socket proxy + fabric sidecar). Does **not** provision the host
  itself — base OS and Docker are the machine owner's responsibility.
  Requires:
  - `services_root`, `operator_user`.
  - `ci_lane_work_dir`, `ci_lane_pnpm_store` — defaulted, but must match the
    matching host entry in the dispatcher's pool config.
  - `ci_lane_runner_image_tag`, `ci_lane_runner_with_android` — defaulted to
    `light` / `false` (no Android SDK, ~10 GB smaller). Set both or neither:
    the role refuses a `light` image carrying the SDK, or a `latest` lacking
    it, because consumers read the tag to decide which job classes a host may
    run. `runner_image` in your pool config must name the tag you set here.

  There is **no `ci_lane_allowed_dispatchers`** and no host firewall. Who may
  reach a lane host's port 2375 is decided by the CI fabric's tailnet ACL,
  enforced by `tailscaled` on the lane host itself — a default-deny packet
  filter admitting only your dispatcher's node to that one port. The role used
  to write `DOCKER-USER` rules as well; they were retired in #3 after being
  measured not to sit on the traffic at all (the proxy runs in the fabric
  sidecar's network namespace, so lane-host traffic never traverses the host's
  FORWARD chain). If you add a machine, the ACL is what you edit.
