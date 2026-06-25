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
