# The controller's compose file still names one operator

**Priority:** medium

`services/ci-controller/compose.yml` is shared by every operator through
`roles/ci_controller`, yet it hardcodes the first operator's machine and layout
(found 2026-11-08 preparing homelab's cut-over):

- `RUNNER_HOST: powerserver` and the fabric sidecar's `hostname: powerserver-dispatcher`;
- `--advertise-tags=tag:ci-dispatcher-alvaro-francisco-gil`;
- `GITHUB_REVIEW_URL: http://github-review:8000`, one consumer's reviewer;
- the work-dir mounts `/mnt/ci-ssd/ci-controller` and
  `/opt/personal/github-actions/ci-controller`, although the role already takes
  `ci_controller_work_dirs` and `ci_controller_cache_root`;
- `/opt/personal/...` for the pool config, `repos.yml`, state and secrets, although the
  role takes `personal_root`.

This is invariant 4 ("no operator's machine is a default") in a place
`test_no_operator_defaults.py` does not read. It works today only because the one
consumer deploying the role is the operator it names.

## Fix

Every value above comes from the `.env` the role already renders (as
`CI_CONTROLLER_IMAGE` does), each with `${VAR:?}` so a missing one stops compose with
its name. Role variables: the dispatcher's host name and fabric tag, the reviewer URL
(optional; empty means no review-aware admission), the work dirs and roots it already
has. A role test asserts the compose file names none of them literally.

## Also

The role pulls only the pool's top-level `runner_image`
(`roles/ci_controller/tasks/main.yml`, last task). A controller host with its own
`runner_image` override under `hosts:` would spawn from an image nobody pulled. Pull
the controller host's effective image instead.
