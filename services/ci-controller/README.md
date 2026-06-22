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

## Config

`personal/ci-controller.yml` (mounted read-only). Key knobs:
- `ram_budget_mb` — the only hard gate. Raise after the RAM upgrade.
- `max_concurrent_lanes` — blunt secondary ceiling against CPU thrash.
- `classes` — per-class `ram_mb`, `needs_kvm`, `needs_android_sdk`, `work_disk`, `group_add`.
- `repos[].label_class` — the allowlist + label→class map (also the ACL).

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
  (shows the ledger, running lanes, and **why** queued jobs are deferred).
- Onboard a repo: `make ci-onboard REPO=owner/name`.
- Deploy: from the main checkout, `... services.yml --tags ci-controller --diff`.
