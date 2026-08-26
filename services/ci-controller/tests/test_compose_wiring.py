import json
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


def test_dispatcher_shares_the_fabric_sidecars_netns_and_the_scheduler_does_not():
    """The dispatcher originates connections to remote lane hosts, so it needs a
    route onto the fabric. The scheduler is dialled BY dispatchers and never dials
    a socket proxy, so it stays where it is -- keeping its blast radius unchanged."""
    compose = _compose()
    assert compose["services"]["ci-controller"]["network_mode"] == "service:ci-fabric"
    assert "networks" not in compose["services"]["ci-controller"]
    assert "network_mode" not in compose["services"]["ci-scheduler"]
    assert compose["services"]["ci-scheduler"]["networks"] == ["homelab"]


def test_fabric_sidecar_uses_a_tun_device_not_userspace():
    """Measured 2026-08-25 against the real fabric: with TS_USERSPACE=true a
    container sharing the netns CANNOT open outbound connections to tailnet IPs --
    curl times out (exit 28) and only tailscaled's own SOCKS5 proxy works. With
    TS_USERSPACE=false plus NET_ADMIN and /dev/net/tun, a tailscale0 interface
    appears in the netns and the same request returns 200.

    The lane host's sidecar stays userspace on purpose: it only ACCEPTS
    connections, which userspace handles, so it needs no NET_ADMIN."""
    sidecar = _compose()["services"]["ci-fabric"]
    assert sidecar["environment"]["TS_USERSPACE"] == "false"
    assert "NET_ADMIN" in sidecar["cap_add"]
    assert any("/dev/net/tun" in d for d in sidecar["devices"])


def test_fabric_sidecar_keeps_docker_dns_and_gains_magicdns():
    """Both must work at once: the dispatcher resolves `ci-scheduler` and
    `docker-socket-proxy` through docker's embedded DNS, and lane hosts through
    MagicDNS. Verified 2026-08-25 -- docker service names still resolve with
    100.100.100.100 configured, and tailnet FQDNs then resolve too. Short tailnet
    names do NOT (no search domain), which is why endpoints are written as FQDNs."""
    sidecar = _compose()["services"]["ci-fabric"]
    assert "100.100.100.100" in sidecar["dns"]
    assert sidecar["networks"]["homelab"]["aliases"] == ["ci-controller"]


def test_fabric_sidecar_reads_its_key_from_a_rendered_env_file():
    """powerserver holds secrets, unlike a lane host, so the key comes from the
    sops-rendered env file. It must never be inlined here -- this file is committed."""
    sidecar = _compose()["services"]["ci-fabric"]
    assert sidecar["env_file"][0]["path"] == "/opt/personal/secrets/ci-fabric.env"
    assert "tskey-" not in json.dumps(sidecar)


def test_no_service_combines_expose_with_a_container_network_mode():
    """Docker rejects the combination outright:

        conflicting options: port exposing and the container type network mode

    and the container fails to CREATE -- so the stack comes up partially, which on
    2026-08-25 left ci-scheduler stopped and the dispatcher unable to schedule.
    `expose` is metadata only; container-to-container traffic never needed it.

    The test that let this through checked network_mode and networks but not
    expose, which is why this asserts the property for every service rather than
    for the one that happened to break."""
    for name, service in _compose()["services"].items():
        if str(service.get("network_mode", "")).startswith("service:"):
            assert "expose" not in service, (
                f"{name}: docker refuses `expose` alongside a container network mode"
            )
            assert "ports" not in service, (
                f"{name}: published ports belong to the namespace owner, not the sharer"
            )


def test_fabric_sidecar_resolves_both_tailnet_and_public_names():
    """The dispatcher needs BOTH, and MagicDNS alone gives only one.

    It polls api.github.com and it dials lane hosts by tailnet FQDN. This tailnet
    has no global nameservers, so 100.100.100.100 answers tailnet names and fails
    everything else -- which on 2026-08-26 took all CI down with every container
    Up and /healthz returning 200, because no repo could be polled at all.

    Order matters: MagicDNS first so tailnet names resolve, a public resolver
    second to catch the SERVFAIL for everything else."""
    dns = _compose()["services"]["ci-fabric"]["dns"]
    assert dns[0] == "100.100.100.100", "MagicDNS must be first or tailnet FQDNs fail"
    assert len(dns) > 1, (
        "a public resolver must follow MagicDNS, or the dispatcher cannot reach GitHub"
    )
    assert not dns[1].startswith("100.100."), "the second entry must be a PUBLIC resolver"
