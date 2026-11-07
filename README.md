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

This repo is the engine; a consumer is any place that runs it: a homelab, a VPS,
a cloud VM. A consumer holds only its configuration (machines, repos, classes,
secrets) and the commit it runs. Every merge to `main` publishes the platform's
images to `ghcr.io/ordago-app/<image>:<commit sha>`
([`.github/workflows/images.yml`](.github/workflows/images.yml)), and the roles
run those images. Nothing is built on a host, so no host holds a copy of the code.
Pin a commit whose `images` run on `main` succeeded: when merges land close
together, GitHub skips the runs queued in between, and those commits have no
images.

## Using the roles

This collection ships three roles. Every play using any of them must set
`become: true` — the source plays had it, the roles assume it, and `become`
cannot be asserted from inside a role.

`ci_controller` and `ci_lane_host` both take:
  - `ci_image_tag` — required: the full commit SHA you pinned this collection to.
    The roles run the images published at that commit, so the code that deploys
    and the code that runs are one commit. Never a branch or `latest`.
  - `ci_image_registry` — where those images live; defaults to
    `ghcr.io/ordago-app`.

- **`ordago.ci.ci_controller`** — the dispatcher (controller, scheduler,
  fabric sidecar). Requires:
  - `services_root`, `personal_root`, `operator_user` — from your inventory.
  - `ci_controller_pool_config` — path to your own `ci-controller.yml`. No
    default; this collection ships no pool config for you. Every `runner_image`
    in it must be `<ci_image_registry>/ci-runner:<ci_image_tag>`, optionally
    with `-light`: the role refuses a pool config that would spawn lanes from
    another commit's image.
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

- **`ordago.ci.github_review`** — one instance of the agentic PR review bot.
  See [GitHub review instances](#github-review-instances) below.

- **`ordago.ci.ci_lane_host`** — offers CI capacity from any machine (scoped
  Docker socket proxy + fabric sidecar). Does **not** provision the host
  itself — base OS and Docker are the machine owner's responsibility.
  Requires:
  - `services_root`, `operator_user`.
  - `ci_lane_work_dir`, `ci_lane_pnpm_store` — defaulted, but must match the
    matching host entry in the dispatcher's pool config.
  - `ci_lane_runner_with_android` — defaulted to `false`: the host pulls
    `ci-runner:<ci_image_tag>-light`, ~10 GB smaller and without the Android
    SDK, so it must allow no class with `needs_android_sdk`. `true` pulls the
    full image.
  - `ci_lane_runner_image` — derived from the above; read it, do not set it.
    `runner_image` for this host in your pool config must equal it.

  There is **no `ci_lane_allowed_dispatchers`** and no host firewall. Who may
  reach a lane host's port 2375 is decided by the CI fabric's tailnet ACL,
  enforced by `tailscaled` on the lane host itself — a default-deny packet
  filter admitting only your dispatcher's node to that one port. The role used
  to write `DOCKER-USER` rules as well; they were retired in #3 after being
  measured not to sit on the traffic at all (the proxy runs in the fabric
  sidecar's network namespace, so lane-host traffic never traverses the host's
  FORWARD chain). If you add a machine, the ACL is what you edit.

## GitHub review instances

`services/github-review` polls the PRs of the repos it is given and posts one
`APPROVE` / `REQUEST_CHANGES` review per head, written by an agent CLI (codex or
Claude Code) running in a throwaway sandbox container. One code tree serves any
number of **instances** — each its own config, accounts, secrets and state, e.g.
an operator's personal reviewer and Órdago's — side by side on one host or not.

**Targets are Docker hosts, not serverless runtimes.** The service mounts the
host's Docker socket to start each review's sandbox, and mounts its clones at
the same path they have on the host (the daemon resolves sandbox mounts by host
path). Any machine running Docker works: a homelab box, a VPS, a cloud VM.
It needs Docker Compose 2.24 or later, for the `env_file` `path:`/`required:`
form the compose file uses.

### Deploying an instance

`ordago.ci.github_review`, once per instance, with `become: true`:

| Variable | Required | What |
|---|---|---|
| `github_review_instance` | yes | `[a-z0-9-]`, unique per host; must equal `instance:` in the config |
| `github_review_root` | yes | absolute host dir holding everything the instance owns |
| `github_review_owner` | yes | host user owning it; git in the service runs as its uid |
| `github_review_config` | yes | controller-side path to this instance's `agent-review.yml` |
| `ci_image_tag` | yes | the full commit SHA you pinned this collection to; the instance runs that commit's images |
| `ci_image_registry` | no | where those images live (default `ghcr.io/ordago-app`) |
| `github_review_network` | no | Docker network `/status` and `/heads` are served on (default `github-review-<instance>`, created if absent) |
| `github_review_secrets` | no | `{VAR: value}` for credentials the config names by `env:` (rendered 0600, never logged) |
| `github_review_secret_files` | no | `{filename: content}` for credentials named by `file:`; seen at `/run/secrets/github-review/<filename>` |
| `github_review_codex_seeds` | no | `{account: auth.json content}`: a codex account's first login, written only while it has none (below) |

The role pulls the service image and both sandbox images (`github-review`,
`github-review-claude`, `github-review-codex`) that this repo published at the
pinned commit, and builds nothing on the host. Nothing comes from the operator's
other services.

A **codex login** is the one credential the role never overwrites: the service
refreshes it in place (its refresh token is single-use), so redeploying it would
roll it back. Make a login used by nothing else — `CODEX_HOME=$(mktemp -d) codex
login --device-auth`, so it is not your own CLI's session — and either pass that
`auth.json` as `github_review_codex_seeds.<account>` (written once, only while
the account has no login on the host) or copy it to
`<github_review_root>/state/codex/<account>/auth.json` (root-owned, 0600). Point
the account at `/var/lib/github-review/codex/<account>/auth.json`. Never give two
instances, or any other process that refreshes, the same login. A seed is spent
once the service first refreshes it; to replace a broken login, delete the
host's file and seed a fresh one.

### Config (`agent-review.yml`)

Accounts are configuration: a repo's `providers` is an ordered failover chain
of declared account names. The first reviews; the next runs only when the one
before it could not run at all (spent quota or credit, bad credentials, a CLI
the vendor's API has moved past). A provider that read the diff and found
problems is never failed over, and neither is one that hit its own spend or
turn cap — that head is given up on and the PR is told why. A chain naming an
undeclared account stops the service at startup; a declared account whose
credential is missing is skipped over at review time with "no credential".

Every Claude account, subscription or API key, runs in the same hardened
container: Read/Glob/Grep only, settings and MCP files from the checkout
ignored, checkout read-only with symlinks neutralised, non-root, no
capabilities, bridge network.

An operator's personal instance:

```yaml
instance: personal
reviewer_bot: my-reviewer[bot]          # the App's bot login; its own PRs are skipped
github_app:
  app_id: 1234567
  private_key: {file: /run/secrets/github-review/reviewer-app.pem}
accounts:
  codex:
    kind: codex
    credential: {file: /var/lib/github-review/codex/codex/auth.json}
    model: ""                           # empty = whatever the subscription serves
  claude-work:
    kind: claude
    auth: subscription                  # CLAUDE_CODE_OAUTH_TOKEN, from `claude setup-token`
    credential: {env: CLAUDE_OAUTH_WORK}
    model: claude-opus-5
  claude-api:
    kind: claude
    auth: api_key                       # ANTHROPIC_API_KEY, spends Console credit
    credential: {env: CLAUDE_API_KEY}
    model: claude-opus-5
    max_budget_usd: 3                   # required for api_key: the per-review cap
repos:
  homelab:                              # key = project slug: names the clone on disk
    repo: someone/homelab
    providers: [codex, claude-api, claude-work]
    review_mode: labeled                # only PRs labelled `ai-review`
```

Órdago's, with a second person's key added by config alone:

```yaml
instance: ordago
reviewer_bot: ordago-reviewer[bot]
github_app:
  app_id: 7654321
  private_key: {file: /run/secrets/github-review/ordago-reviewer.pem}
accounts:
  claude-api-jaime:
    kind: claude
    auth: api_key
    credential: {env: CLAUDE_API_KEY_JAIME}
    model: claude-opus-5
    max_budget_usd: 3
  claude-api-alvaro:
    kind: claude
    auth: api_key
    credential: {env: CLAUDE_API_KEY_ALVARO}
    model: claude-opus-5
    max_budget_usd: 3
repos:
  ordago-apps:
    repo: ordago-app/ordago-apps
    providers: [claude-api-jaime, claude-api-alvaro]
    review_mode: labeled
    review_dependency_bots: true        # labelled Dependabot/Renovate bumps get a dependency review
    run_ci_first: false                 # review in parallel with CI
limits:                                 # all optional; defaults shown
  poll_interval_seconds: 60
  concurrency: 3
  max_attempts: 3
  retry_backoff_seconds: 900
  max_rounds: 5                         # REQUEST_CHANGES verdicts per PR
  max_runs: 10                          # paid provider runs per PR
  max_restart_refunds: 2
  stuck_running_seconds: 10800          # must exceed max_runtime_minutes x longest chain
```

Per repo, also: `enabled` (true), `review_drafts` (false), `review_dependency_bots`
(false), `run_ci_first` (true), `review_mode` (`all` | `labeled`, default `all`),
`max_runtime_minutes` (30). Unknown keys are refused, not ignored.

**What each account spent.** Every paid run is logged (`job N owner/repo#PR:
<account> ran, cost $X`) and recorded per account in `jobs.db`'s `review_runs`,
and `/status` returns the totals for the last day and 30 days under `spend`. The
cost is the Claude CLI's own `total_cost_usd`, failed runs included; Codex
reports none, so its runs count in `runs` but not in `priced_runs`. Read it on
the host with `docker exec github-review-<instance> python -c "import
urllib.request;print(urllib.request.urlopen('http://localhost:8000/status').read().decode())"`.

The GitHub App needs Pull requests: write, Contents: read, Checks: read and
Metadata: read, installed on every account that owns a reviewed repo. Tokens
are minted in the service, one per repo, scoped to that repo.

### Secrets (names only — values live in each operator's own store)

What the Órdago example above reads, as a `secrets.example.yml` would list it:

```yaml
github_review_secrets:
  CLAUDE_API_KEY_JAIME: sk-ant-api03-REPLACE_ME    # Console key, in a workspace with a spend limit
  CLAUDE_API_KEY_ALVARO: sk-ant-api03-REPLACE_ME
github_review_secret_files:
  ordago-reviewer.pem: REPLACE_ME                  # the reviewer App's private key, PEM
```

A subscription account takes `sk-ant-oat01-…` from `claude setup-token` the same
way. Nothing in this repo holds a value: it is public, and a ciphertext
committed here stays readable to anyone who later gets a key.

