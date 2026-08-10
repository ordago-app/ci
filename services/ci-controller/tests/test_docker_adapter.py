import itertools
import re
from collections import defaultdict
from unittest.mock import MagicMock

import docker.errors
from src.config import ControllerConfig
from src.docker_adapter import (
    CLASS_LABEL,
    HOST_LABEL,
    JOB_LABEL,
    LANE_LABEL,
    DockerAdapter,
    DockerPool,
)
from src.models import AdmitDecision, QueuedJob

from tests.conftest import VALID_CONFIG


def _adapter(write_config, host="powerserver", host_config=None, suffix_factory=None):
    """Adapter with a deterministic lane-id suffix, so tests can assert exact ids.

    Production takes the adapter's own random suffix; the tests that care about it
    build a DockerAdapter directly instead of going through here.
    """
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    client = MagicMock()
    resolved = host_config or cfg.resolved_hosts()[host]
    counter = itertools.count(1)
    adapter = DockerAdapter(
        client=client,
        config=cfg,
        host=host,
        host_config=resolved,
        suffix_factory=suffix_factory or (lambda: f"{next(counter):06x}"),
    )
    return adapter, client, cfg


def test_spawn_light_lane(write_config) -> None:
    adapter, client, _ = _adapter(write_config)
    job = QueuedJob(
        job_id=42, repo="alvaro-francisco-gil/homelab", labels=["self-hosted", "homelab"]
    )
    decision = AdmitDecision(job=job, class_name="light", ram_mb=700, needs_kvm=False)

    lane_id, _container_id = adapter.spawn(decision, registration_token="ARRT")

    assert lane_id == "powerserver-cici-42-000001"
    _, kwargs = client.containers.run.call_args
    assert kwargs["name"] == "github-runner-powerserver-cici-42-000001"
    assert kwargs["network"] == "homelab"
    assert kwargs["auto_remove"] is True
    assert kwargs["labels"] == {
        LANE_LABEL: "powerserver-cici-42-000001",
        JOB_LABEL: "42",
        CLASS_LABEL: "light",
        HOST_LABEL: "powerserver",
    }
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
    assert kwargs["environment"]["RUNNER_WORKDIR"] == "/runner-base/powerserver-cici-42-000001-work"
    assert kwargs["environment"]["RUNNER_NAME"] == "powerserver-cici-42-000001"


def _volume_mode_kwargs(write_config):
    """Spawn one lane on a host configured work_dir_mode=volume."""
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    resolved = cfg.resolved_hosts()["powerserver"].model_copy(update={"work_dir_mode": "volume"})
    adapter, client, _ = _adapter(write_config, host_config=resolved)
    decision = AdmitDecision(
        job=QueuedJob(job_id=42, repo="alvaro-francisco-gil/homelab", labels=["self-hosted"]),
        class_name="light",
        ram_mb=700,
        needs_kvm=False,
    )
    adapter.spawn(decision, registration_token="ARRT")
    _, kwargs = client.containers.run.call_args
    return kwargs


def test_volume_mode_gives_the_lane_an_anonymous_volume_docker_can_reclaim(
    write_config,
) -> None:
    """Source=None is the entire point: it is what makes the volume per-container.

    A NAMED volume would survive the lane and need an explicit delete through the
    VOLUMES capability, which the lane-host socket proxy denies — so it would leak
    exactly like the bind mount this mode replaces, only less visibly.
    """
    kwargs = _volume_mode_kwargs(write_config)

    mounts = kwargs["mounts"]
    assert len(mounts) == 1, f"expected exactly one lane volume, got {mounts}"
    spec = dict(mounts[0])
    assert spec["Type"] == "volume"
    assert spec["Source"] is None, (
        f"a named volume outlives its lane and the proxy denies VOLUMES: {spec}"
    )
    assert spec["Target"] == "/runner-work"
    # auto_remove is what actually drops the anonymous volume with the container.
    assert kwargs["auto_remove"] is True


def test_volume_mode_does_not_bind_a_host_work_dir(write_config) -> None:
    """The leak was the host path itself, so the mode must remove it, not add beside it.

    Caches are a separate concern (shared_mounts) and must survive the switch: a
    volume-mode lane still needs its pnpm/gradle stores or every job refetches.
    """
    kwargs = _volume_mode_kwargs(write_config)

    binds = kwargs["volumes"]
    assert "/runner-base" not in [v["bind"] for v in binds.values()], (
        f"volume mode must not bind a host work dir: {binds}"
    )
    assert kwargs["environment"]["RUNNER_WORKDIR"].startswith("/runner-work/"), (
        "the workspace must live inside the volume, not beside it"
    )
    # ...and the caches are still there.
    assert binds["/mnt/ci-ssd/pnpm-store"] == {"bind": "/cache/pnpm", "mode": "rw"}


def test_bind_mode_sends_no_mounts_at_all(write_config) -> None:
    """powerserver's containers must stay byte-identical to before this option existed."""
    adapter, client, _ = _adapter(write_config)
    decision = AdmitDecision(
        job=QueuedJob(job_id=42, repo="alvaro-francisco-gil/homelab", labels=["self-hosted"]),
        class_name="light",
        ram_mb=700,
        needs_kvm=False,
    )
    adapter.spawn(decision, registration_token="ARRT")
    _, kwargs = client.containers.run.call_args

    assert "mounts" not in kwargs, (
        "an empty Mounts list is still a payload change on the host that does not need it"
    )


def test_spawn_uses_the_hosts_own_runner_image(write_config) -> None:
    """A lane host's image is not powerserver's, so spawn must not hardcode the latter.

    The lane host builds :light (no Android SDK, which its allowed_classes forbid it
    from needing). Running the top-level :latest tag there 404s -- the proxy denies
    IMAGES and BUILD, so the controller cannot pull or build a missing image.
    """
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    host_config = cfg.resolved_hosts()["powerserver"].model_copy(
        update={"runner_image": "homelab/github-actions-runner:light"}
    )
    adapter, client, _ = _adapter(write_config, host_config=host_config)
    job = QueuedJob(
        job_id=7, repo="alvaro-francisco-gil/homelab", labels=["self-hosted", "homelab"]
    )
    decision = AdmitDecision(job=job, class_name="light", ram_mb=700, needs_kvm=False)

    adapter.spawn(decision, registration_token="T")

    args, _ = client.containers.run.call_args
    assert args[0] == "homelab/github-actions-runner:light"


def test_spawn_falls_back_to_the_top_level_runner_image(write_config) -> None:
    """A host that names no image of its own runs the shared one."""
    adapter, client, cfg = _adapter(write_config)
    job = QueuedJob(
        job_id=8, repo="alvaro-francisco-gil/homelab", labels=["self-hosted", "homelab"]
    )
    adapter.spawn(
        AdmitDecision(job=job, class_name="light", ram_mb=700, needs_kvm=False),
        registration_token="T",
    )
    args, _ = client.containers.run.call_args
    assert args[0] == cfg.runner_image


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
    assert kwargs["environment"]["RUNNER_WORKDIR"] == "/runner-base/powerserver-cici-7-000001-work"


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
    # No class label: this container predates CLASS_LABEL, so its class is unknown
    # rather than assumed — the controller prices unknowns at the largest class.
    assert lanes[0].class_name is None
    _, kwargs = client.containers.list.call_args
    assert kwargs["filters"] == {"label": LANE_LABEL}


def test_spawn_stamps_lane_identity_on_the_container(write_config) -> None:
    adapter, client, _ = _adapter(write_config)
    client.containers.run.return_value = MagicMock(id="deadbeef")
    job = QueuedJob(
        job_id=7,
        repo="alvaro-francisco-gil/homelab",
        labels=["self-hosted", "homelab"],
        workflow="CI",
        job_name="pytest",
    )
    decision = AdmitDecision(job=job, class_name="light", ram_mb=700, needs_kvm=False)

    lane_id, container_id = adapter.spawn(decision, registration_token="ARRT")

    labels = client.containers.run.call_args.kwargs["labels"]
    assert (lane_id, container_id) == ("powerserver-cici-7-000001", "deadbeef")
    assert labels[CLASS_LABEL] == "light"
    assert labels[HOST_LABEL] == "powerserver"


def test_two_lanes_for_the_same_job_get_distinct_identities(write_config) -> None:
    """A job whose lane was stolen is re-admitted while the first lane still runs. Both
    lanes must be able to coexist: same ledger, same Docker daemon, same runner names."""
    adapter, client, _ = _adapter(write_config)
    job = QueuedJob(job_id=7, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    decision = AdmitDecision(job=job, class_name="light", ram_mb=700, needs_kvm=False)

    first, _ = adapter.spawn(decision, registration_token="ARRT")
    second, _ = adapter.spawn(decision, registration_token="ARRT")

    calls = client.containers.run.call_args_list
    names = [c.kwargs["name"] for c in calls]
    workdirs = [c.kwargs["environment"]["RUNNER_WORKDIR"] for c in calls]
    assert first != second
    assert len(set(names)) == 2
    assert len(set(workdirs)) == 2


def test_lane_id_keeps_the_host_and_job_prefix(write_config) -> None:
    """The prefix is what makes a lane greppable back to the job that motivated it."""
    adapter, _client, _ = _adapter(write_config)
    job = QueuedJob(job_id=7, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    decision = AdmitDecision(job=job, class_name="light", ram_mb=700, needs_kvm=False)

    lane_id, _ = adapter.spawn(decision, registration_token="ARRT")

    assert lane_id.startswith("powerserver-cici-7-")


def test_the_default_suffix_is_unique_per_spawn(write_config) -> None:
    """Production injects no suffix_factory, so the built-in one must not repeat."""
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    client = MagicMock()
    adapter = DockerAdapter(
        client=client,
        config=cfg,
        host="powerserver",
        host_config=cfg.resolved_hosts()["powerserver"],
    )
    job = QueuedJob(job_id=7, repo="alvaro-francisco-gil/homelab", labels=["homelab"])
    decision = AdmitDecision(job=job, class_name="light", ram_mb=700, needs_kvm=False)

    ids = {adapter.spawn(decision, registration_token="ARRT")[0] for _ in range(50)}

    assert len(ids) == 50
    assert all(re.fullmatch(r"powerserver-cici-7-[0-9a-f]{6}", lane_id) for lane_id in ids)


def test_list_lanes_reads_the_class_label(write_config) -> None:
    adapter, client, _ = _adapter(write_config)
    container = MagicMock()
    container.id = "def456"
    container.labels = {
        LANE_LABEL: "powerserver-cici-11",
        JOB_LABEL: "11",
        CLASS_LABEL: "emulator",
    }
    client.containers.list.return_value = [container]

    lanes = adapter.list_lanes()

    assert lanes[0].class_name == "emulator"


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


def test_spawn_labels_the_lane_with_its_class(write_config) -> None:
    """reconcile() must be able to recover a lane's real class after a restart."""
    adapter, client, _ = _adapter(write_config)
    decision = AdmitDecision(
        job=QueuedJob(job_id=7, repo="alvaro-francisco-gil/homelab", labels=["homelab"]),
        class_name="light",
        ram_mb=700,
        needs_kvm=False,
        work_disk="ssd",
    )

    adapter.spawn(decision, registration_token="tok")

    assert client.containers.run.call_args.kwargs["labels"][CLASS_LABEL] == "light"


def test_list_lanes_reports_the_labelled_class(write_config) -> None:
    adapter, client, _ = _adapter(write_config)
    container = MagicMock()
    container.id = "cid"
    container.labels = {LANE_LABEL: "p-cici-7", JOB_LABEL: "7", CLASS_LABEL: "emulator"}
    client.containers.list.return_value = [container]

    (lane,) = adapter.list_lanes()

    assert lane.class_name == "emulator"


def test_list_lanes_tolerates_a_lane_spawned_before_the_class_label(write_config) -> None:
    """Lanes already running at deploy time carry no class label; that must not crash."""
    adapter, client, _ = _adapter(write_config)
    container = MagicMock()
    container.id = "cid"
    container.labels = {LANE_LABEL: "p-cici-8", JOB_LABEL: "8"}
    client.containers.list.return_value = [container]

    (lane,) = adapter.list_lanes()

    assert lane.class_name is None


def test_cpu_shares_omitted_when_host_config_has_none(write_config) -> None:
    """powerserver's containers must be created byte-identically to today: no cpu_shares."""
    adapter, client, _ = _adapter(write_config)  # powerserver host_config: cpu_shares is None
    decision = AdmitDecision(
        job=QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"]),
        class_name="light",
        ram_mb=700,
        needs_kvm=False,
    )

    adapter.spawn(decision, registration_token="tok")

    assert "cpu_shares" not in client.containers.run.call_args.kwargs


def test_cpu_shares_present_when_host_config_sets_it(write_config) -> None:
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    host_config = cfg.resolved_hosts()["powerserver"].model_copy(update={"cpu_shares": 128})
    adapter, client, _ = _adapter(write_config, host_config=host_config)
    decision = AdmitDecision(
        job=QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"]),
        class_name="light",
        ram_mb=700,
        needs_kvm=False,
    )

    adapter.spawn(decision, registration_token="tok")

    assert client.containers.run.call_args.kwargs["cpu_shares"] == 128


def test_spawn_labels_the_lane_with_its_host(write_config) -> None:
    cfg = ControllerConfig.load(write_config(VALID_CONFIG))
    host_config = cfg.resolved_hosts()["powerserver"].model_copy(update={"name": "powervaro-ci"})
    adapter, client, _ = _adapter(write_config, host="powervaro-ci", host_config=host_config)
    decision = AdmitDecision(
        job=QueuedJob(job_id=1, repo="alvaro-francisco-gil/homelab", labels=["homelab"]),
        class_name="light",
        ram_mb=700,
        needs_kvm=False,
    )

    adapter.spawn(decision, registration_token="tok")

    assert client.containers.run.call_args.kwargs["labels"][HOST_LABEL] == "powervaro-ci"


def test_list_lanes_reports_the_labelled_host(write_config) -> None:
    adapter, client, _ = _adapter(write_config)
    container = MagicMock()
    container.id = "cid"
    container.labels = {LANE_LABEL: "p-cici-7", JOB_LABEL: "7", HOST_LABEL: "powervaro-ci"}
    client.containers.list.return_value = [container]

    (lane,) = adapter.list_lanes()

    assert lane.host == "powervaro-ci"


def test_list_lanes_tolerates_a_lane_spawned_before_the_host_label(write_config) -> None:
    """Same tolerance as class_name: lanes predating HOST_LABEL must not crash."""
    adapter, client, _ = _adapter(write_config)
    container = MagicMock()
    container.id = "cid"
    container.labels = {LANE_LABEL: "p-cici-8", JOB_LABEL: "8"}
    client.containers.list.return_value = [container]

    (lane,) = adapter.list_lanes()

    assert lane.host is None


def test_ping_true_when_daemon_responds(write_config) -> None:
    adapter, client, _ = _adapter(write_config)
    client.ping.return_value = True

    assert adapter.ping() is True


def test_ping_false_on_docker_exception(write_config) -> None:
    adapter, client, _ = _adapter(write_config)
    client.ping.side_effect = docker.errors.DockerException("dead daemon")

    assert adapter.ping() is False


def test_ping_false_on_connection_error(write_config) -> None:
    """An unreachable tailnet address (e.g. a sleeping desktop) raises this, not a docker error."""
    adapter, client, _ = _adapter(write_config)
    client.ping.side_effect = ConnectionError("no route to host")

    assert adapter.ping() is False


HOSTS_CONFIG = """\
ram_budget_mb: 12000
max_concurrent_lanes: 8
default_class: light
default_host: powerserver
runner_image: homelab/github-actions-runner:latest
work_dirs:
  ssd: /mnt/ci-ssd/ci-controller
  hdd: /opt/personal/github-actions/ci-controller
classes:
  light:
    ram_mb: 700
    work_disk: ssd
repos:
  - repo: alvaro-francisco-gil/homelab
    label_class:
      homelab: light
hosts:
  powerserver:
    docker_endpoint: tcp://docker-socket-proxy:2375
  powervaro-ci:
    docker_endpoint: tcp://100.64.0.1:2375
    cpu_shares: 128
"""


def test_docker_pool_builds_an_adapter_per_host(write_config) -> None:
    cfg = ControllerConfig.load(write_config(HOSTS_CONFIG))
    # Pre-created, because clients are now built lazily on first use: a factory that
    # populated `clients` on call would leave the dict empty until a host is touched.
    clients = defaultdict(MagicMock)

    def factory(endpoint: str):
        return clients[endpoint]

    pool = DockerPool(cfg, client_factory=factory)

    assert isinstance(pool.for_host("powerserver"), DockerAdapter)
    assert isinstance(pool.for_host("powervaro-ci"), DockerAdapter)
    # An adapter per host, but no client behind any of them yet — see
    # test_pool_construction_never_contacts_a_host for why that matters.
    assert set(clients) == set()


def test_docker_pool_for_host_unknown_raises_key_error(write_config) -> None:
    cfg = ControllerConfig.load(write_config(HOSTS_CONFIG))
    pool = DockerPool(cfg, client_factory=lambda endpoint: MagicMock())

    import pytest

    with pytest.raises(KeyError):
        pool.for_host("nonexistent")


def test_docker_pool_healthy_reports_only_hosts_that_ping(write_config) -> None:
    cfg = ControllerConfig.load(write_config(HOSTS_CONFIG))
    # Pre-created, because clients are now built lazily on first use: a factory that
    # populated `clients` on call would leave the dict empty until a host is touched.
    clients = defaultdict(MagicMock)

    def factory(endpoint: str):
        return clients[endpoint]

    pool = DockerPool(cfg, client_factory=factory)
    clients["tcp://docker-socket-proxy:2375"].ping.return_value = True
    clients["tcp://100.64.0.1:2375"].ping.side_effect = docker.errors.DockerException("asleep")

    healthy, _lanes = pool.snapshot()
    assert healthy == {"powerserver"}


def test_docker_pool_snapshot_skips_down_hosts(write_config) -> None:
    cfg = ControllerConfig.load(write_config(HOSTS_CONFIG))
    # Pre-created, because clients are now built lazily on first use: a factory that
    # populated `clients` on call would leave the dict empty until a host is touched.
    clients = defaultdict(MagicMock)

    def factory(endpoint: str):
        return clients[endpoint]

    pool = DockerPool(cfg, client_factory=factory)
    up = clients["tcp://docker-socket-proxy:2375"]
    down = clients["tcp://100.64.0.1:2375"]
    up.ping.return_value = True
    up.containers.list.return_value = []
    down.ping.side_effect = ConnectionError("unreachable")

    healthy, lanes = pool.snapshot()

    assert set(lanes) == {"powerserver"}
    assert lanes["powerserver"] == []
    # Health and lanes come from the same pass, so they can never disagree.
    assert healthy == set(lanes)


def test_pool_construction_never_contacts_a_host(write_config) -> None:
    """Building the pool must not touch the network.

    Production incident: docker.DockerClient contacts the daemon inside its own
    constructor (_retrieve_server_version), so building every client eagerly meant one
    unreachable lane host raised out of DockerPool.__init__ — before the controller
    reached the health check that exists to skip exactly that host. The controller
    crash-looped at startup and took CI down with it. Every other test injected a fake
    factory, so none of them ever ran the real constructor.
    """
    cfg = ControllerConfig.load(write_config(HOSTS_CONFIG))
    calls: list[str] = []

    def factory(endpoint: str):
        calls.append(endpoint)
        return MagicMock()

    pool = DockerPool(cfg, client_factory=factory)

    assert calls == [], "no client may be built until a host is actually used"

    pool.for_host("powerserver").ping()
    assert calls == ["tcp://docker-socket-proxy:2375"], "only the used host's client is built"


def test_an_unreachable_host_reports_unhealthy_instead_of_raising(write_config) -> None:
    """The real failure path, through the real default factory.

    A host whose name does not resolve must degrade to ping()==False, not an exception.
    `.invalid` is reserved by RFC 2606 and can never resolve, so this needs no network.
    """
    cfg = ControllerConfig.load(
        write_config(
            HOSTS_CONFIG.replace(
                "tcp://100.64.0.1:2375", "tcp://powervaro-ci.does-not-exist.invalid:2375"
            )
        )
    )

    pool = DockerPool(cfg)  # no factory: the real docker.DockerClient path

    healthy, lanes = pool.snapshot()
    assert "powervaro-ci" not in healthy
    assert "powervaro-ci" not in lanes


def test_a_host_that_wakes_up_rejoins_the_pool_on_a_later_tick(write_config) -> None:
    """A failed client build must NOT be cached — the whole point of an opportunistic host.

    The desktop is expected to sleep and wake. If the first failed construction were
    remembered, that host would report unhealthy forever and only a controller restart
    would recover it — silently converting "the desktop was asleep for an hour" into
    "the second host never comes back". Lazy construction alone does not guarantee this:
    an implementation that stored the failure would pass every other test here.
    """
    cfg = ControllerConfig.load(write_config(HOSTS_CONFIG))
    live = MagicMock()
    live.ping.return_value = True
    live.containers.list.return_value = []
    asleep = {"desktop": True}

    def factory(endpoint: str):
        if endpoint == "tcp://100.64.0.1:2375" and asleep["desktop"]:
            raise docker.errors.DockerException("host is asleep")
        return live

    pool = DockerPool(cfg, client_factory=factory)

    healthy, _lanes = pool.snapshot()
    assert "powervaro-ci" not in healthy, "asleep host must be unhealthy"

    asleep["desktop"] = False  # the desktop wakes up
    healthy, lanes = pool.snapshot()

    assert "powervaro-ci" in healthy, "a woken host must rejoin without a controller restart"
    assert lanes["powervaro-ci"] == []
