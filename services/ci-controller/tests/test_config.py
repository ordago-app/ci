from pathlib import Path

import pytest
from src.config import ConfigError, ControllerConfig

from tests.conftest import VALID_CONFIG


def test_loads_valid_config(write_config) -> None:
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    assert cfg.ram_budget_mb == 12000
    assert cfg.max_concurrent_lanes == 8
    assert cfg.classes["emulator"].needs_kvm is True
    assert cfg.classes["light"].work_disk == "ssd"
    assert cfg.repo_names() == {
        "alvaro-francisco-gil/ordago-apps",
        "alvaro-francisco-gil/homelab",
    }


def test_class_for_maps_label(write_config) -> None:
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    assert (
        cfg.class_for("alvaro-francisco-gil/ordago-apps", ["self-hosted", "android-e2e"])
        == "emulator"
    )
    assert (
        cfg.class_for("alvaro-francisco-gil/ordago-apps", ["self-hosted", "ordago-ci"]) == "light"
    )


def test_class_for_falls_back_to_default(write_config) -> None:
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    assert cfg.class_for("alvaro-francisco-gil/homelab", ["self-hosted", "linux"]) == "light"


def test_class_for_returns_none_for_unlisted_repo(write_config) -> None:
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    assert cfg.class_for("someone/other-repo", ["self-hosted"]) is None


def test_class_for_rejects_hosted_runner_job(write_config) -> None:
    # A GitHub-hosted job (e.g. dependabot on ubuntu-latest) has no "self-hosted"
    # label and no mapped label — the controller must NOT admit it (it can't run it).
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    assert cfg.class_for("alvaro-francisco-gil/homelab", ["ubuntu-latest"]) is None


def test_class_for_self_hosted_unmapped_label_uses_default(write_config) -> None:
    # A self-hosted job whose custom label isn't mapped still gets the default class.
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    assert cfg.class_for("alvaro-francisco-gil/homelab", ["self-hosted", "x64"]) == "light"


def test_default_class_must_exist(write_config) -> None:
    bad = VALID_CONFIG.replace("default_class: light", "default_class: nonexistent")
    with pytest.raises(ConfigError, match="default_class"):
        ControllerConfig.load(write_config(bad))


def test_label_class_must_reference_known_class(write_config) -> None:
    bad = VALID_CONFIG.replace("android-e2e: emulator", "android-e2e: bogus")
    with pytest.raises(ConfigError, match="bogus"):
        ControllerConfig.load(write_config(bad))


def test_invalid_work_disk_rejected(write_config) -> None:
    bad = VALID_CONFIG.replace("work_disk: ssd", "work_disk: floppy")
    with pytest.raises(ConfigError, match="work_disk"):
        ControllerConfig.load(write_config(bad))


def test_missing_file_raises_config_error() -> None:
    with pytest.raises(ConfigError):
        ControllerConfig.load(Path("/nonexistent/ci-controller.yml"))
