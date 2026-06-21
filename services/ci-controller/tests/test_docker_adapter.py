from unittest.mock import MagicMock

from src.config import ControllerConfig
from src.docker_adapter import JOB_LABEL, LANE_LABEL, DockerAdapter
from src.models import AdmitDecision, QueuedJob

from tests.conftest import VALID_CONFIG


def _adapter(write_config):
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    client = MagicMock()
    return DockerAdapter(client=client, config=cfg, host="powerserver"), client, cfg


def test_spawn_light_lane(write_config) -> None:
    adapter, client, _ = _adapter(write_config)
    job = QueuedJob(
        job_id=42, repo="alvaro-francisco-gil/homelab", labels=["self-hosted", "homelab"]
    )
    decision = AdmitDecision(job=job, class_name="light", ram_mb=700, needs_kvm=False)

    lane_id = adapter.spawn(decision, registration_token="ARRT")

    assert lane_id == "powerserver-cici-42"
    _, kwargs = client.containers.run.call_args
    assert kwargs["name"] == "github-runner-powerserver-cici-42"
    assert kwargs["network"] == "homelab"
    assert kwargs["auto_remove"] is True
    assert kwargs["labels"] == {LANE_LABEL: "powerserver-cici-42", JOB_LABEL: "42"}
    assert kwargs["environment"]["RUNNER_REGISTRATION_TOKEN"] == "ARRT"
    assert kwargs["environment"]["RUNNER_REPOSITORY"] == "alvaro-francisco-gil/homelab"
    assert kwargs["environment"]["RUNNER_EPHEMERAL"] == "1"
    assert kwargs["environment"]["RUNNER_LABELS"] == "self-hosted,homelab"
    assert kwargs["environment"]["SKIP_ANDROID_SDK"] == "1"  # light class: no SDK
    # lane_env from config is merged in (cache-path parity with the static runner pool)
    assert kwargs["environment"]["PNPM_HOME"] == "/cache/pnpm"
    assert kwargs["environment"]["GRADLE_USER_HOME"] == "/cache/gradle"
    assert "devices" not in kwargs or not kwargs["devices"]
    # work dir on ssd
    assert any(
        "/mnt/ci-ssd/ci-controller/powerserver-cici-42-work" in host for host in kwargs["volumes"]
    )


def test_spawn_emulator_lane_gets_kvm(write_config) -> None:
    adapter, client, _ = _adapter(write_config)
    job = QueuedJob(
        job_id=7, repo="alvaro-francisco-gil/ordago-apps", labels=["self-hosted", "android-e2e"]
    )
    decision = AdmitDecision(job=job, class_name="emulator", ram_mb=2500, needs_kvm=True)

    adapter.spawn(decision, registration_token="TOK")

    _, kwargs = client.containers.run.call_args
    assert kwargs["devices"] == ["/dev/kvm:/dev/kvm:rwm"]
    assert kwargs["group_add"] == ["994"]
    assert kwargs["environment"]["SKIP_ANDROID_SDK"] == "0"
    assert any(
        "/opt/personal/github-actions/ci-controller/powerserver-cici-7-work" in h
        for h in kwargs["volumes"]
    )


def test_list_lanes(write_config) -> None:
    adapter, client, _ = _adapter(write_config)
    container = MagicMock()
    container.id = "abc123"
    container.labels = {LANE_LABEL: "powerserver-cici-9", JOB_LABEL: "9"}
    client.containers.list.return_value = [container]

    lanes = adapter.list_lanes()

    assert len(lanes) == 1
    assert lanes[0].lane_id == "powerserver-cici-9"
    assert lanes[0].job_id == 9
    assert lanes[0].container_id == "abc123"
    _, kwargs = client.containers.list.call_args
    assert kwargs["filters"] == {"label": LANE_LABEL}


def test_remove_ignores_missing(write_config) -> None:
    import docker.errors

    adapter, client, _ = _adapter(write_config)
    client.containers.get.side_effect = docker.errors.NotFound("gone")
    adapter.remove("ghost")  # must not raise
