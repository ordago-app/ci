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
  - `ci_lane_allowed_dispatchers` — non-empty list of dispatcher
    hostnames/addresses this lane host accepts connections from.
  - `ci_lane_work_dir`, `ci_lane_pnpm_store` — defaulted, but must match the
    matching host entry in the dispatcher's pool config.
