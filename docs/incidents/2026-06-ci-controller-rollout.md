# ci-controller rollout

## Symptom

`ci-controller` minted installation tokens fine but every `GET /repos/{repo}/actions/runs?status=queued`
poll returned 403, so it discovered no queued jobs. Separately, once polling worked, real
`ordago-ci` jobs failed at the "Set up job" step with empty steps on controller-spawned lanes,
while the same jobs succeeded on the old static runner pool.

## Root cause

The `github_actions_runner_app` GitHub App (installation `138699175`) was scoped only for
runner registration (`Administration: Read/Write`), not for reading queued Actions runs. And
`docker_adapter.spawn` only passed `RUNNER_*` env plus a few shared mounts — it did not
replicate the static pool's full environment (`GRADLE_USER_HOME`, `ANDROID_HOME` /
`ANDROID_SDK_ROOT`, `ANDROID_AVD_HOME`, `PNPM_HOME`, `MAESTRO_CLI_NO_ANALYTICS`), so lanes
came up without the tooling jobs needed.

## Fix

Granted the App `Actions: Read` and approved the new permission on the installation (no
redeploy needed — the controller polls every 15s and self-recovers). Made `docker_adapter.spawn`
replicate the static pool's env + cache mounts.

## What still bites

- The App (installation `138699175`) needs `Actions: Read` — if it's ever reinstalled or a new
  App is swapped in, this scope must be granted again or every queue poll silently 403s.
- `ansible ... --tags ci-controller` does **not** rebuild the runner image. Pair a controller
  deploy with `--tags github-actions-runner`, or force it with
  `-e ci_controller_rebuild_runner_image=true`, whenever the runner image needs to change.
- Any future adapter that spawns runner containers (not just `docker_adapter`) must replicate
  the static pool's full env + cache mounts or jobs fail at "Set up job" with no useful error.
