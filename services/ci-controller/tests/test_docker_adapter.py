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
    # work dir: bind the SSD base (owned by uid 1000) and let the runner create its own
    # per-lane subdir inside it — avoids docker auto-creating a root-owned per-lane dir.
    assert kwargs["volumes"]["/mnt/ci-ssd/ci-controller"] == {"bind": "/runner-base", "mode": "rw"}
    assert kwargs["environment"]["RUNNER_WORKDIR"] == "/runner-base/powerserver-cici-42-work"


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
    # emulator lane: HDD base bound; runner creates its per-lane subdir inside it
    assert kwargs["volumes"]["/opt/personal/github-actions/ci-controller"] == {
        "bind": "/runner-base",
        "mode": "rw",
    }
    assert kwargs["environment"]["RUNNER_WORKDIR"] == "/runner-base/powerserver-cici-7-work"


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


def test_sample_parses_ram_and_cpu(write_config) -> None:
    adapter, client, _ = _adapter(write_config)
    container = MagicMock()
    container.stats.return_value = {
        "memory_stats": {"usage": 2961178624, "stats": {"inactive_file": 0}},  # 2824 MiB
        "cpu_stats": {
            "cpu_usage": {"total_usage": 2000000000},
            "system_cpu_usage": 100000000000,
            "online_cpus": 8,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 1000000000},
            "system_cpu_usage": 99000000000,
        },
    }
    client.containers.get.return_value = container
    ram, cpu = adapter.sample("abc")
    assert ram == 2824
    assert round(cpu, 1) == 800.0  # (1e9 delta / 1e9 system delta) * 8 cpus * 100


def test_sample_excludes_reclaimable_page_cache(write_config) -> None:
    """A lane whose usage is mostly page cache must not report it as RAM.

    Regression: CI lanes charged the git checkout + shared pnpm-store reads to
    their cgroup, so `usage` showed >12 GB for jobs that only run `grep`.
    """
    adapter, client, _ = _adapter(write_config)
    container = MagicMock()
    container.stats.return_value = {
        "memory_stats": {
            "usage": 13421772800,  # 12800 MiB total charged to the cgroup
            "stats": {"inactive_file": 13212057600},  # 12600 MiB of it page cache
        },
        "cpu_stats": {
            "cpu_usage": {"total_usage": 2000000000},
            "system_cpu_usage": 100000000000,
            "online_cpus": 8,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 1000000000},
            "system_cpu_usage": 99000000000,
        },
    }
    client.containers.get.return_value = container
    ram, _ = adapter.sample("abc")
    assert ram == 200


def test_sample_survives_missing_inactive_file(write_config) -> None:
    """Older daemons omit memory_stats.stats — fall back to raw usage, not a crash."""
    adapter, client, _ = _adapter(write_config)
    container = MagicMock()
    container.stats.return_value = {
        "memory_stats": {"usage": 1073741824},  # 1024 MiB, no `stats` key
        "cpu_stats": {
            "cpu_usage": {"total_usage": 2000000000},
            "system_cpu_usage": 100000000000,
            "online_cpus": 8,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 1000000000},
            "system_cpu_usage": 99000000000,
        },
    }
    client.containers.get.return_value = container
    ram, _ = adapter.sample("abc")
    assert ram == 1024


def test_sample_returns_none_when_missing(write_config) -> None:
    import docker.errors

    adapter, client, _ = _adapter(write_config)
    client.containers.get.side_effect = docker.errors.NotFound("gone")
    assert adapter.sample("ghost") is None
