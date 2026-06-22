from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "compose.yml"


def test_metrics_db_volume_and_env() -> None:
    spec = yaml.safe_load(COMPOSE.read_text())
    svc = spec["services"]["ci-controller"]
    assert any("/var/lib/ci-controller" in v for v in svc["volumes"])
    assert svc["environment"]["CI_CONTROLLER_DB"] == "/var/lib/ci-controller/metrics.db"
