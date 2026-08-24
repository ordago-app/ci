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


def test_disk_budget_references_known_disk(write_config) -> None:
    bad = VALID_CONFIG.replace("work_dirs:", "disk_budget_gb:\n  floppy: 10\nwork_dirs:")
    with pytest.raises(ConfigError, match="disk_budget_gb"):
        ControllerConfig.load(write_config(bad))


def test_disk_budget_and_work_gb_load(write_config) -> None:
    text = VALID_CONFIG.replace(
        "work_dirs:", "disk_budget_gb:\n  ssd: 6\n  hdd: 300\nwork_dirs:"
    ).replace("    work_disk: ssd\n", "    work_disk: ssd\n    work_gb: 4\n")
    cfg = ControllerConfig.load(write_config(text))
    assert cfg.disk_budget_gb == {"ssd": 6, "hdd": 300}
    assert cfg.classes["light"].work_gb == 4
    assert cfg.classes["emulator"].work_gb == 0  # default when unspecified


def test_missing_file_raises_config_error() -> None:
    with pytest.raises(ConfigError):
        ControllerConfig.load(Path("/nonexistent/ci-controller.yml"))


def test_config_version_is_stable_and_sensitive(write_config) -> None:
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    same = ControllerConfig.load(write_config(VALID_CONFIG))
    assert cfg.config_version() == same.config_version()
    assert len(cfg.config_version()) == 8
    changed = ControllerConfig.load(
        write_config(VALID_CONFIG.replace("ram_budget_mb: 12000", "ram_budget_mb: 15000"))
    )
    assert changed.config_version() != cfg.config_version()


def test_admission_mode_defaults_and_validates(write_config) -> None:
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    assert cfg.admission_mode == "reservation"
    bad = VALID_CONFIG.replace(
        "ram_budget_mb: 12000", "admission_mode: bogus\nram_budget_mb: 12000"
    )
    with pytest.raises(ConfigError, match="admission_mode"):
        ControllerConfig.load(write_config(bad))


def test_idle_reaper_defaults(write_config) -> None:
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    assert (cfg.idle_grace_seconds, cfg.idle_lane_max_seconds) == (60.0, 600.0)


def test_grace_must_be_below_the_absolute_cap(write_config) -> None:
    with pytest.raises(ConfigError):
        ControllerConfig.load(
            write_config(VALID_CONFIG + "idle_grace_seconds: 900\nidle_lane_max_seconds: 600\n")
        )


def test_absent_hosts_key_synthesises_one_host_from_the_top_level(write_config) -> None:
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    hosts = cfg.resolved_hosts()
    assert list(hosts) == ["powerserver"]
    only = hosts["powerserver"]
    assert only.ram_budget_mb == cfg.ram_budget_mb
    assert only.max_concurrent_lanes == cfg.max_concurrent_lanes
    assert only.work_dirs == cfg.work_dirs
    assert only.disk_budget_gb == cfg.disk_budget_gb
    assert only.host_free_ram_floor_mb == cfg.host_free_ram_floor_mb
    assert only.allowed_classes == sorted(cfg.classes)  # omitted => every class
    assert only.cpu_shares is None  # omitted => docker's default weight


HOSTS_CONFIG = (
    VALID_CONFIG
    + """
default_host: powerserver
hosts:
  powerserver:
    docker_endpoint: tcp://docker-socket-proxy:2375
  powervaro-ci:
    docker_endpoint: tcp://powervaro.REDACTED-PERSONAL-TAILNET.ts.net:2375
    allowed_classes: [light]
    cpu_shares: 256
    max_concurrent_lanes: 3
    host_free_ram_floor_mb: 8000
    work_dirs:
      ssd: /var/lib/ci-lanes
      hdd: /var/lib/ci-lanes
"""
)


def test_per_host_keys_override_and_the_rest_inherit(write_config) -> None:
    cfg = ControllerConfig.load(write_config(HOSTS_CONFIG))
    desktop = cfg.resolved_hosts()["powervaro-ci"]
    assert desktop.max_concurrent_lanes == 3  # overridden
    assert desktop.host_free_ram_floor_mb == 8000  # overridden
    assert desktop.ram_budget_mb == cfg.ram_budget_mb  # inherited
    assert desktop.host_load_ceiling == cfg.host_load_ceiling
    assert desktop.allowed_classes == ["light"]
    assert desktop.cpu_shares == 256
    # runner_image is omitted here, so it must fall back to the top-level image.
    # A host silently getting None would make DockerAdapter run "None" and 404.
    assert desktop.runner_image == cfg.runner_image


def test_a_host_may_override_the_runner_image(write_config) -> None:
    """A lane host runs an image built without the Android SDK it cannot schedule.

    The socket proxy denies IMAGES and BUILD, so the controller can only run what
    ansible already built there -- which for a light-only host is a different tag
    from powerserver's. Without a per-host override the controller would name
    powerserver's tag on a host that does not have it, 404ing every admission.
    """
    cfg = ControllerConfig.load(
        write_config(
            HOSTS_CONFIG.replace(
                "    allowed_classes: [light]",
                "    allowed_classes: [light]\n    runner_image: homelab/x:light",
            )
        )
    )
    assert cfg.resolved_hosts()["powervaro-ci"].runner_image == "homelab/x:light"
    # ...and the override must not leak onto the other host.
    assert cfg.resolved_hosts()["powerserver"].runner_image == cfg.runner_image


def test_unknown_class_in_allowed_classes_is_rejected(write_config) -> None:
    bad = (
        VALID_CONFIG
        + """
hosts:
  powerserver: {docker_endpoint: "tcp://p:2375", allowed_classes: [nope]}
"""
    )
    with pytest.raises(ConfigError):
        ControllerConfig.load(write_config(bad))


def test_default_host_must_be_in_the_hosts_map(write_config) -> None:
    bad = HOSTS_CONFIG.replace("default_host: powerserver", "default_host: ghost")
    with pytest.raises(ConfigError):
        ControllerConfig.load(write_config(bad))


def test_unknown_work_dir_mode_is_rejected(write_config) -> None:
    """Only 'bind' and 'volume' exist, and a typo must not fall back to either.

    Silently defaulting an unrecognised value to `bind` would put a REMOTE host back on
    host work dirs the controller cannot delete — the leak of ADR 0023, reintroduced by a
    misspelling. Defaulting it to `volume` would be worse on powerserver, moving lanes off
    the NVMe. Neither guess is safe, so the config must refuse to load.
    """
    bad = HOSTS_CONFIG.replace(
        "    allowed_classes: [light]", "    work_dir_mode: tmpfs\n    allowed_classes: [light]"
    )
    with pytest.raises(ConfigError, match="work_dir_mode"):
        ControllerConfig.load(write_config(bad))


def test_work_dir_mode_defaults_to_bind(write_config) -> None:
    """Absent means bind, so a config written before this option behaves exactly as it did.

    powerserver relies on this default rather than stating it.
    """
    cfg = ControllerConfig.load(write_config(HOSTS_CONFIG))
    assert cfg.resolved_hosts()["powerserver"].work_dir_mode == "bind"


OPERATOR_CONFIG = Path(__file__).resolve().parents[3] / "personal" / "ci-controller.yml"


def test_operator_config_loads() -> None:
    # personal/ci-controller.yml is copied to the host verbatim and only parsed at
    # container start, so a schema error there surfaces as a crash-looping controller
    # after deploy rather than as a failing check. Parse it here instead.
    cfg = ControllerConfig.load(OPERATOR_CONFIG)
    assert cfg.ram_budget_mb > 0
    assert cfg.default_class in cfg.classes


def test_operator_config_never_underprices_a_mapped_label() -> None:
    # class_for falls through to default_class for any self-hosted label it does not
    # recognise, and default_class is the cheapest class. A label that is mapped in one
    # repo but missing in another therefore silently books the cheapest reserve for a
    # job that may need many times that — the same under-reserve shape as the readopt
    # bug. Pin every mapped label so a rename cannot half-land.
    cfg = ControllerConfig.load(OPERATOR_CONFIG)
    for repo in cfg.repos:
        for label, class_name in repo.label_class.items():
            assert class_name in cfg.classes, f"{repo.repo}: '{label}' -> unknown '{class_name}'"
            assert cfg.class_for(repo.repo, ["self-hosted", label]) == class_name


def test_repos_are_resolved_through_the_shared_registry(write_config) -> None:
    """`repos:` names projects; the GitHub owner/name comes from
    personal/repos.yml, so a repo that changes owner is one edit in one file."""
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    assert cfg.repo_names() == {
        "alvaro-francisco-gil/ordago-apps",
        "alvaro-francisco-gil/homelab",
    }
    assert cfg.class_for("alvaro-francisco-gil/homelab", ["self-hosted", "homelab"]) == "light"


def test_a_project_missing_from_the_registry_is_rejected(tmp_path) -> None:
    """Skipping it would stop provisioning lanes for that repo, which presents
    as jobs queueing forever with nothing in the logs."""
    (tmp_path / "repos.yml").write_text("project_repos:\n  homelab: personal/homelab\n")
    path = tmp_path / "ci-controller.yml"
    path.write_text(VALID_CONFIG)
    with pytest.raises(ConfigError, match="ordago-apps"):
        ControllerConfig.load(path)


def test_a_registry_entry_that_is_not_owner_name_is_rejected(tmp_path) -> None:
    (tmp_path / "repos.yml").write_text("project_repos:\n  homelab: homelab\n  ordago-apps: a/b\n")
    path = tmp_path / "ci-controller.yml"
    path.write_text(VALID_CONFIG)
    with pytest.raises(ConfigError, match="owner/name"):
        ControllerConfig.load(path)


def test_the_operator_config_and_registry_agree() -> None:
    """personal/ci-controller.yml and personal/repos.yml are copied to the host
    together; a project in one and not the other crash-loops the controller."""
    cfg = ControllerConfig.load(OPERATOR_CONFIG)
    assert "alvaro-francisco-gil/ordago-apps" in cfg.repo_names()
