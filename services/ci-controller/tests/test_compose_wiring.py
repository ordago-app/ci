from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "compose.yml"


def test_metrics_db_volume_and_env() -> None:
    spec = yaml.safe_load(COMPOSE.read_text())
    svc = spec["services"]["ci-controller"]
    assert any("/var/lib/ci-controller" in v for v in svc["volumes"])
    assert svc["environment"]["CI_CONTROLLER_DB"] == "/var/lib/ci-controller/metrics.db"


def test_work_dir_bases_mounted_rw_for_reap_cleanup() -> None:
    # The controller deletes each reaped lane's <lane_id>-work dir, so the work_dirs
    # bases must be bind-mounted into the controller (rw) at their host paths.
    spec = yaml.safe_load(COMPOSE.read_text())
    volumes = spec["services"]["ci-controller"]["volumes"]
    for base in ("/mnt/ci-ssd/ci-controller", "/opt/personal/github-actions/ci-controller"):
        assert any(v == f"{base}:{base}:rw" for v in volumes), (
            f"work-dir base {base} must be mounted rw so the controller can prune lane dirs"
        )


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def test_scheduler_service_exists_and_holds_no_github_credentials():
    """The scheduler is the component that will one day serve two organisations. If it
    ever gains an env_file carrying a GitHub App key, the whole split is pointless."""
    scheduler = _compose()["services"]["ci-scheduler"]

    assert "env_file" not in scheduler
    env = scheduler.get("environment", {})
    assert not [k for k in env if "GITHUB" in k.upper()]


def test_controller_points_at_the_scheduler():
    controller = _compose()["services"]["ci-controller"]
    assert controller["environment"]["CI_SCHEDULER_URL"] == "http://ci-scheduler:8001"
    assert "ci-scheduler" in controller["depends_on"]


def test_scheduler_does_not_reach_the_docker_socket():
    """Credential-free is not enough: a scheduler that can reach a socket proxy could
    spawn containers on every host in the pool."""
    scheduler = _compose()["services"]["ci-scheduler"]
    assert "volumes" not in scheduler or not any(
        "docker.sock" in v for v in scheduler.get("volumes", [])
    )
    assert "DOCKER_PROXY_URL" not in scheduler.get("environment", {})


def test_scheduler_command_overrides_the_image_cmd_to_run_the_scheduler():
    """The image's default CMD runs the dispatcher (src.main); without this override
    ci-scheduler would start the dispatcher, which fails immediately for lack of
    GitHub credentials."""
    scheduler = _compose()["services"]["ci-scheduler"]
    assert scheduler["command"] == ["python", "-m", "src.scheduler_main"]


def test_scheduler_exposes_its_port():
    scheduler = _compose()["services"]["ci-scheduler"]
    assert "8001" in scheduler.get("expose", [])
