# github-actions-runner

Compose-managed GitHub Actions runner pools for selected private repositories.

The pool serves `alvaro-francisco-gil/ordago-apps` and is split by capacity into
**1 heavy + 4 light** runners (the box is an i7-4770, 4 cores / 15 GiB — one
Android emulator alone wants 4 cores + 4 GB, so only one emulator may run at a
time):

| Service | Runner name | Labels | KVM | Work dir | Role |
| --- | --- | --- | --- | --- | --- |
| `ordago-android-e2e`   | `powerserver-ordago-android-e2e`   | `android-e2e, ordago-ci` | yes | HDD | Heavy — Android E2E; also joins the light pool when no emulator runs |
| `ordago-android-e2e-2` | `powerserver-ordago-android-e2e-2` | `ordago-ci` | no | SSD | Light |
| `ordago-android-e2e-3` | `powerserver-ordago-android-e2e-3` | `ordago-ci` | no | SSD | Light |
| `ordago-android-e2e-4` | `powerserver-ordago-android-e2e-4` | `ordago-ci` | no | SSD | Light (lean, `SKIP_ANDROID_SDK=1`) |
| `ordago-android-e2e-5` | `powerserver-ordago-android-e2e-5` | `ordago-ci` | no | SSD | Light (lean, `SKIP_ANDROID_SDK=1`) |

Service names ending `-android-e2e-N` on the light runners are legacy; the
labels (not the names) determine routing.

### Why 5 (concurrency cap)

The heavy runner's working set lives on a **7,200 rpm HDD**; on a single spinning
disk, concurrent CI hits **100% I/O utilization** (random seeks) while the CPU
sits idle, so more HDD lanes made the queue *slower*. The fix was to move the
light runners' `/runner-work` onto a **30 GB SSD** (`/mnt/ci-ssd`): flash absorbs
the concurrent random I/O the HDD couldn't. With 4 light lanes on the SSD + 1
heavy on the HDD, the binding constraint becomes **RAM (15 GiB)** — ~5 light jobs
fit (no concurrent emulator); 6+ lanes would need a memory upgrade. SSD capacity
(4 light work dirs × ~3 GB ≈ 12–16 GB of 30 GB, plus one shared pnpm store
≈ 2–5 GB) has headroom. The next unlock is a larger SSD + more RAM.

### pnpm binary + store

The pnpm **binary** is baked into the image (`ARG PNPM_VERSION`, the standalone
release from `github.com/pnpm/pnpm` — self-contained, independent of the system
Node). Ordago CI used to provision it per job via `pnpm/action-setup`, which
re-downloaded pnpm from the npm registry every run; that download flaked on this
box (`curl error 23` → ENOENT → "self-installer exits with code 254"), failing
the "Setup PNPM" step before any code ran. Self-hosted-pool jobs therefore
**omit `pnpm/action-setup`** and rely on the baked binary; keep `PNPM_VERSION` in
lockstep with ordago-apps `package.json` `"packageManager"` (a mismatch makes
pnpm self-provision that version at job time, re-introducing the download until
this image is rebuilt). GitHub-hosted (`ubuntu-latest`) ordago jobs keep
`action-setup` — they don't run here.

The image also sets pnpm `store-dir=/cache/pnpm` (a user-level `.npmrc`). Without it,
`PNPM_HOME=/cache/pnpm` only relocated the global bin — pnpm kept its store on
the container layer and the mounted volume sat empty, so every job re-fetched
dependencies. The **light pool shares one store at `/mnt/ci-ssd/pnpm-store`**:
it lives on the same disk as their `/runner-work`, so `pnpm install` hardlinks
from the store instead of copying across disks, and one shared store maximizes
cache hits. The heavy runner keeps its store on the HDD next to its HDD work
dir (same-disk hardlinks). Because the store is now genuinely persistent,
workflows on this pool should **not** also use `actions/setup-node`'s
`cache: pnpm` — that tars the store up and re-extracts it every job, the
redundant I/O the persistent store exists to avoid.

Concurrent `pnpm install` reads/hardlinks from a shared store are safe, but
simultaneous *writes* into one store under heavy parallelism can occasionally
hit `Cannot link`/index races. If that surfaces, the escape hatch is
**per-runner stores on the SSD** (each co-located with that runner's
`/runner-work`): point each light runner's `/cache/pnpm` at its own
`/mnt/ci-ssd/ordago-light-N-pnpm` instead of the shared path. ~3 GB × 4 ≈ 12 GB
still fits the 30 GB SSD; it trades cache-hit rate for zero write contention.

## Deployment

This is **deployed by ansible** (`make deploy host=powerserver`), gated by
`github-actions-runner` in [`personal/services-enabled.yml`](../../personal/services-enabled.yml).
The pool topology lives in [`personal/github-runners.yml`](../../personal/github-runners.yml),
which names each runner's *project*; the project's GitHub `owner/name` comes
from [`personal/repos.yml`](../../personal/repos.yml).

`services.yml` renders `compose.yml` from [`compose.yml.j2`](compose.yml.j2),
creates the per-runner work/cache dirs, and brings the stack up. Secrets live
outside the repo at `/opt/personal/secrets/github-actions-runner.env`, rendered
from `github_actions_runner_app` in `secrets/secrets.prod.yml`.

To add or resize the pool, edit `personal/github-runners.yml` (each runner is a
list entry) and re-deploy — no hand-edited compose on the host.

## Safety

Self-hosted runner jobs execute arbitrary shell commands inside the runner
container. Do not route deploy/release jobs here until a separate pool is
designed for that trust boundary.

The runner GitHub App secret is used only by `entrypoint.sh` to mint short-lived
installation, registration, and remove tokens. The entrypoint unsets the app
environment variables before starting `run.sh` so workflow jobs do not inherit
them.

## Labels

Jobs route by label:

```yaml
# Android E2E job (emulator) — heavy runner only:
runs-on: [self-hosted, linux, x64, powerserver, android-e2e]

# Every other CI job — any runner in the light pool (incl. the heavy when idle):
runs-on: [self-hosted, linux, x64, powerserver, ordago-ci]
```

No workflow should use only `self-hosted`.

## State

Host state lives under the pool's `work_root` from
`personal/github-runners.yml`. For `ordago-android-e2e`:

```text
/opt/personal/github-actions/ordago-android-e2e/
├── work/
├── gradle/
├── android-sdk/
├── android-avd/
└── pnpm-store/
```

These directories are persistent caches, not source of truth.

## First Deploy Checklist

1. Create a dedicated GitHub App owned by the operator account.
2. Disable webhooks; this service only uses outbound REST API calls.
3. Grant repository `Administration: Read and Write`; this is required to mint
   self-hosted runner registration/remove tokens.
4. Install the App on every **account** owning a repository listed in
   `personal/repos.yml` — personal and org alike. The installation id is never
   configured: both the entrypoint and `ci-controller` resolve it per repo, so
   one App serves repos across owners.
5. Store `app_id` and the downloaded PEM private key in
   `secrets/secrets.prod.yml` under `github_actions_runner_app`.
6. Confirm root disk has enough headroom for Android SDK and AVD caches:

   ```bash
   ssh powerbot@powerserver 'df -h / && docker system df'
   ```

7. Deploy only after reviewing the Ansible check output:

   ```bash
   ansible-playbook -i inventory/hosts.yml ansible/playbooks/services.yml \
     --limit powerserver \
     --tags github-actions-runner \
     --check --diff

   make deploy host=powerserver
   ```

8. Confirm the runner appears online in GitHub:
   `ordago-apps` -> Settings -> Actions -> Runners.

9. Do not route deploy/release workflows to this pool.
