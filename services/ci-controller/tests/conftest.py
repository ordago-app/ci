from pathlib import Path

import pytest


@pytest.fixture()
def write_config(tmp_path: Path):
    """Write controller YAML to a temp file and return its path.

    The repo registry (personal/repos.yml) is written beside it, which is where
    ControllerConfig.load looks: `repos:` entries name a project, and the
    project's GitHub `owner/name` lives in exactly one file for the whole host."""

    def _write(text: str) -> Path:
        (tmp_path / "repos.yml").write_text(
            "project_repos:\n"
            "  homelab: alvaro-francisco-gil/homelab\n"
            "  ordago-apps: ordago-app/ordago-apps\n"
            "  cultuvilla: alvaro-francisco-gil/cultuvilla\n"
        )
        path = tmp_path / "ci-controller.yml"
        path.write_text(text)
        return path

    return _write


VALID_CONFIG = """\
ram_budget_mb: 12000
max_concurrent_lanes: 8
default_class: light
default_host: powerserver
runner_image: homelab/github-actions-runner:latest
work_dirs:
  ssd: /mnt/ci-ssd/ci-controller
  hdd: /opt/personal/github-actions/ci-controller
shared_mounts:
  - { host: /mnt/ci-ssd/pnpm-store, container: /cache/pnpm }
lane_env:
  PNPM_HOME: /cache/pnpm
  GRADLE_USER_HOME: /cache/gradle
classes:
  emulator:
    ram_mb: 2500
    needs_kvm: true
    needs_android_sdk: true
    work_disk: hdd
    group_add: ["994"]
  light:
    ram_mb: 700
    work_disk: ssd
repos:
  - project: ordago-apps
    label_class:
      android-e2e: emulator
      ordago-ci: light
  - project: homelab
    label_class:
      homelab: light
"""
