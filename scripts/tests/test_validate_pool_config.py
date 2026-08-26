import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "validate_pool_config.py"

VALID = """\
ram_budget_mb: 12000
max_concurrent_lanes: 8
default_class: light
default_host: fixture-host
runner_image: fixture/runner:latest
work_dirs: {ssd: /tmp/ssd, hdd: /tmp/hdd}
classes:
  light: {ram_mb: 700, work_disk: ssd}
repos:
  - project: demo
    label_class: {demo-ci: light}
"""
REGISTRY = "project_repos:\n  demo: acme/demo\n"


def _run(tmp_path: Path, config: str, registry: str = REGISTRY) -> subprocess.CompletedProcess:
    (tmp_path / "repos.yml").write_text(registry)
    (tmp_path / "pool.yml").write_text(config)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "pool.yml"), str(tmp_path / "repos.yml")],
        capture_output=True,
        text=True,
    )


def test_a_valid_config_exits_zero(tmp_path: Path) -> None:
    result = _run(tmp_path, VALID)
    assert result.returncode == 0
    assert result.stderr == ""


def test_a_config_missing_default_host_is_rejected_by_name(tmp_path: Path) -> None:
    broken = "\n".join(line for line in VALID.splitlines() if not line.startswith("default_host:"))
    result = _run(tmp_path, broken)
    assert result.returncode == 1
    # The consumer sees only this message; it has to name the field.
    assert "default_host" in result.stderr


def test_a_config_with_unknown_default_class_is_rejected_by_name(tmp_path: Path) -> None:
    broken = VALID.replace("default_class: light", "default_class: nonexistent")
    result = _run(tmp_path, broken)
    assert result.returncode == 1
    assert "default_class" in result.stderr
    assert "nonexistent" in result.stderr


def test_a_repo_missing_from_the_registry_is_rejected_by_project_name(tmp_path: Path) -> None:
    # This is the one failure mode ControllerConfig.load itself introduces on top
    # of pydantic validation: the config is schema-valid but references a project
    # with no entry in repos.yml.
    result = _run(tmp_path, VALID, registry="project_repos: {}\n")
    assert result.returncode == 1
    assert "demo" in result.stderr


def test_malformed_yaml_is_rejected_not_crashed(tmp_path: Path) -> None:
    result = _run(tmp_path, "ram_budget_mb: [unterminated\n")
    assert result.returncode == 1
    assert result.stderr != ""


def test_wrong_argument_count_exits_two_with_usage(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "pool.yml")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Usage" in result.stderr
