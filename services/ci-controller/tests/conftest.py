from pathlib import Path

import pytest


@pytest.fixture()
def write_config(tmp_path: Path):
    """Write controller YAML to a temp file and return its path."""

    def _write(text: str) -> Path:
        path = tmp_path / "ci-controller.yml"
        path.write_text(text)
        return path

    return _write


VALID_CONFIG = """\
ram_budget_mb: 12000
max_concurrent_lanes: 8
default_class: light
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
  - repo: alvaro-francisco-gil/ordago-apps
    label_class:
      android-e2e: emulator
      ordago-ci: light
  - repo: alvaro-francisco-gil/homelab
    label_class:
      homelab: light
"""
