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
(4 light work dirs × ~3 GB ≈ 12–16 GB of 30 GB) has headroom. The next unlock is
a larger SSD + more RAM.

## Deployment (current)

This is **deployed manually** to `/opt/services/github-actions-runner/` on
`powerserver` (`docker compose up -d`); secrets live outside the repo at
`/opt/personal/secrets/github-actions-runner.env`. The Ansible "First Deploy
Checklist" below is the intended future flow and is not wired up yet — edit the
files here and copy them to the host, or `docker compose` on the host directly.

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
4. Install the App only on repositories listed in `personal/github-runners.yml`.
5. Store `app_id`, `installation_id`, and the downloaded PEM private key in
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
