from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "compose.yml"
SERVICES_PLAYBOOK = Path(__file__).resolve().parents[3] / "ansible" / "playbooks" / "services.yml"


def _load() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def _ci_controller_tasks() -> list[dict]:
    play = yaml.safe_load(SERVICES_PLAYBOOK.read_text())[0]
    return play["tasks"]


def test_controller_has_no_raw_socket() -> None:
    compose = _load()
    volumes = compose["services"]["ci-controller"].get("volumes", [])
    msg = "controller must never mount the raw docker socket"
    assert all("docker.sock" not in v for v in volumes), msg


def test_socket_proxy_mounts_socket_read_only() -> None:
    compose = _load()
    volumes = compose["services"]["docker-socket-proxy"]["volumes"]
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in volumes


def test_socket_proxy_is_scoped() -> None:
    compose = _load()
    env = compose["services"]["docker-socket-proxy"]["environment"]
    # Allowed: containers + POST (to create/start). Denied: exec, images write, etc.
    assert env["CONTAINERS"] == 1
    assert env["POST"] == 1
    assert env["EVENTS"] == 1  # read-only event stream: intentional, not a mutation surface
    for denied in ("EXEC", "IMAGES", "VOLUMES", "NETWORKS", "SECRETS", "SWARM", "SERVICES"):
        assert env.get(denied, 0) == 0, f"{denied} must be denied on the socket proxy"


def test_controller_exposes_only() -> None:
    compose = _load()
    svc = compose["services"]["ci-controller"]
    assert "ports" not in svc, "controller must not publish ports (no public ingress)"
    assert "8000" in [str(p) for p in svc["expose"]]


def test_both_on_homelab_network() -> None:
    compose = _load()
    for name in ("ci-controller", "docker-socket-proxy"):
        assert "homelab" in compose["services"][name]["networks"]


def test_prune_workdir_timer_and_service_are_deployed() -> None:
    # Backstop for the in-controller reap cleanup: a systemd service + daily timer
    # prune stale *-work dirs. Pin them so the unit/timer/command can't silently drift.
    tasks = _ci_controller_tasks()
    by_dest = {}
    for t in tasks:
        copy = t.get("ansible.builtin.copy") or t.get("copy")
        if copy and "dest" in copy:
            by_dest[copy["dest"]] = copy.get("content", "")

    svc = by_dest.get("/etc/systemd/system/ci-controller-prune-workdirs.service")
    timer = by_dest.get("/etc/systemd/system/ci-controller-prune-workdirs.timer")
    assert svc is not None, "prune service unit must be deployed"
    assert timer is not None, "prune timer unit must be deployed"

    # The prune command must target both work-dir bases and only *-work dirs older than a day.
    assert "/mnt/ci-ssd/ci-controller" in svc
    assert "/opt/personal/github-actions/ci-controller" in svc
    assert "-name '*-work'" in svc
    assert "-mtime +1" in svc
    assert "OnCalendar=daily" in timer

    # The timer must be enabled+started via systemd with a daemon-reload.
    enabled = any(
        (t.get("ansible.builtin.systemd") or t.get("systemd") or {}).get("name")
        == "ci-controller-prune-workdirs.timer"
        for t in tasks
    )
    assert enabled, "prune timer must be enabled/started via systemd"
