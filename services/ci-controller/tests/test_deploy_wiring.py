from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
COMPOSE = Path(__file__).resolve().parents[1] / "compose.yml"
SERVICES_PLAYBOOK = REPO / "ansible" / "playbooks" / "services.yml"
GROUP_VARS = REPO / "inventory" / "group_vars" / "all.yml"
LANE_HOST_COMPOSE = REPO / "services" / "ci-lane-host" / "compose.yml"
LANE_HOST_PLAYBOOK = REPO / "ansible" / "playbooks" / "ci-lane-host.yml"
CONTROLLER_CONFIG = REPO / "personal" / "ci-controller.yml"


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


# ── ci-lane-host: the opportunistic second host's proxy and work dirs ──


def _lane_host_compose() -> dict:
    return yaml.safe_load(LANE_HOST_COMPOSE.read_text())


def _lane_host_tasks() -> list[dict]:
    return yaml.safe_load(LANE_HOST_PLAYBOOK.read_text())[0]["tasks"]


def test_lane_host_proxy_capabilities_match_the_controllers_exactly() -> None:
    """A lane host's proxy is powerserver's, copied unchanged.

    Granting a capability here that powerserver does not grant would widen the
    blast radius of a remote, network-published socket — the one place it must
    not be wider. Compared as a whole dict so an ADDED key fails too, not just a
    changed value.
    """
    assert (
        _lane_host_compose()["services"]["docker-socket-proxy"]["environment"]
        == _load()["services"]["docker-socket-proxy"]["environment"]
    )


def test_lane_host_proxy_mounts_the_socket_read_only() -> None:
    volumes = _lane_host_compose()["services"]["docker-socket-proxy"]["volumes"]
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in volumes


def test_lane_host_proxy_never_binds_all_interfaces() -> None:
    """Port 2375 is an unauthenticated container-create API.

    Reachable from anywhere it is a root shell on the operator's desktop, so the
    published port must name a bind address and that address must not be a wildcard.
    The real value is rendered into .env by ansible, which discovers the tailnet
    address among the host's IPs (mirrored in from Windows — this host runs no
    tailscaled of its own). The bind address is necessary but NOT sufficient; see
    test_lane_host_restricts_the_socket_proxy_at_the_firewall.
    """
    (published,) = _lane_host_compose()["services"]["docker-socket-proxy"]["ports"]
    host_part = str(published).rsplit(":", 2)[0]
    assert host_part not in ("", "0.0.0.0", "::", "[::]"), (
        f"lane-host proxy publishes {published!r} without a specific bind address"
    )
    assert "CI_LANE_BIND_ADDR" in host_part, "bind address must come from the ansible-rendered .env"


def test_lane_host_playbook_creates_every_configured_work_dir() -> None:
    """The controller binds work_dirs straight into the lane container.

    A path the playbook does not create is a path docker auto-creates root-owned,
    and the uid-1000 runner then cannot write its workspace — every lane on this
    host fails at "Set up job".
    """
    config = yaml.safe_load(CONTROLLER_CONFIG.read_text())
    configured = set(config["hosts"]["powervaro-ci"]["work_dirs"].values())

    tasks = _lane_host_tasks()
    play_vars = yaml.safe_load(LANE_HOST_PLAYBOOK.read_text())[0].get("vars", {})
    created = set()
    for task in tasks:
        file_mod = task.get("ansible.builtin.file") or task.get("file")
        if file_mod and file_mod.get("state") == "directory":
            path = str(file_mod["path"])
            # Resolve the one play-level var the work-dir task uses.
            for name, value in play_vars.items():
                path = path.replace(f"{{{{ {name} }}}}", str(value))
            created.add(path)

    assert configured <= created, f"work dirs not created by the playbook: {configured - created}"


def test_lane_host_playbook_builds_the_runner_image_locally() -> None:
    """The proxy denies IMAGES and BUILD, so the controller can never install it.

    Without a locally-built image every admission to this host 404s, which reads
    like a controller bug and is not one.
    """
    tasks = _lane_host_tasks()
    built = [
        t.get("community.docker.docker_image")
        for t in tasks
        if t.get("community.docker.docker_image")
    ]
    assert any(
        img.get("name") == "homelab/github-actions-runner" and img.get("source") == "build"
        for img in built
    ), "ci-lane-host.yml must build homelab/github-actions-runner locally"


def test_lane_host_prunes_its_own_stale_work_dirs() -> None:
    """The controller skips work-dir cleanup for remote lanes (it cannot see that
    filesystem), so this timer is the host's ONLY cleanup, not a backstop."""
    by_dest = {}
    for task in _lane_host_tasks():
        copy = task.get("ansible.builtin.copy") or task.get("copy")
        if copy and "dest" in copy:
            by_dest[copy["dest"]] = copy.get("content", "")

    svc = by_dest.get("/etc/systemd/system/ci-lane-prune-workdirs.service")
    timer = by_dest.get("/etc/systemd/system/ci-lane-prune-workdirs.timer")
    assert svc is not None and timer is not None
    assert "-name '*-work'" in svc
    assert "-mtime +1" in svc
    assert "OnCalendar=daily" in timer


def test_lane_hosts_are_excluded_from_the_services_stack() -> None:
    """services.yml renders every secret env file. A lane host must never receive
    them — that isolation is the whole reason the desktop can host lanes at all.
    Structural (`all:!ci_hosts`), not a convention about always passing --limit."""
    play = yaml.safe_load(LANE_HOST_PLAYBOOK.read_text())[0]
    assert play["hosts"] == "ci_hosts"

    # The full policy, pinned so it cannot drift one playbook at a time. A lane host
    # gets base (hardening, operator user — loads secrets but writes none to the host)
    # and docker; it is excluded from every play that puts a secret ON the target or
    # installs anything beyond docker + sshd + the socket-proxy.
    playbooks = SERVICES_PLAYBOOK.parent
    expected = {
        "services.yml": "all:!ci_hosts",
        "devtools.yml": "all:!ci_hosts",
        "tailscale.yml": "all:!ci_hosts",
        "backup.yml": "all:!ci_hosts",
        "base.yml": "all",
        "docker.yml": "all",
    }
    actual = {name: yaml.safe_load((playbooks / name).read_text())[0]["hosts"] for name in expected}
    assert actual == expected

    inventory = yaml.safe_load((REPO / "inventory" / "hosts.yml").read_text())
    children = inventory["all"]["children"]
    assert "powervaro-ci" in children["ci_hosts"]["hosts"]
    assert "powervaro-ci" not in children["homelab"]["hosts"]


def test_operator_config_is_copied_to_the_host() -> None:
    # personal/ci-controller.yml is the only place lane classes and their reserves are
    # defined, and the controller reads it from a read-only bind mount. If the copy task
    # loses its dest, the container keeps running against whatever stale file is on the
    # host and a reserve change silently never takes effect.
    personal_root = yaml.safe_load(GROUP_VARS.read_text())["personal_root"]
    tasks = _ci_controller_tasks()
    dests = []
    for t in tasks:
        copy = t.get("ansible.builtin.copy") or t.get("copy")
        if copy and "src" in copy and "ci-controller.yml" in str(copy["src"]):
            dests.append(str(copy["dest"]).replace("{{ personal_root }}", personal_root))
    assert dests, "personal/ci-controller.yml must be copied to the host"

    compose = _load()
    mounts = compose["services"]["ci-controller"]["volumes"]
    config_env = compose["services"]["ci-controller"]["environment"]["CI_CONTROLLER_CONFIG"]
    for dest in dests:
        assert any(m.startswith(f"{dest}:") for m in mounts), f"{dest} must be mounted"
        assert any(m == f"{dest}:{config_env}:ro" for m in mounts), (
            f"{dest} must be mounted read-only at {config_env}"
        )


def test_documented_provisioning_sequence_installs_the_lane_plays_prerequisites() -> None:
    """`ci-lane-host.yml` uses ansible.posix.synchronize, which needs rsync on the target.

    rsync is installed by base.yml, so a runbook that tells the operator to run only
    docker.yml before the lane-host play fails at the first synchronize task on a fresh
    WSL distro — and silently skips the base hardening too. Pin both the requirement and
    the documented sequence so they cannot drift apart again.
    """
    lane_play_text = LANE_HOST_PLAYBOOK.read_text()
    if "synchronize" not in lane_play_text:
        return  # no rsync dependency; nothing to guarantee

    base = yaml.safe_load((SERVICES_PLAYBOOK.parent / "base.yml").read_text())[0]
    installed = yaml.dump(base)
    assert "rsync" in installed, "base.yml is the play this sequence relies on for rsync"

    runbook = (REPO / "docs" / "runbook.md").read_text()
    section = runbook.split("## Second CI lane host")[1].split("\n## ")[0]
    for play in ("base.yml", "docker.yml", "ci-lane-host.yml"):
        assert play in section, f"provisioning sequence must name {play}"
    assert section.index("base.yml") < section.index("ci-lane-host.yml"), (
        "base.yml must be run before the lane-host play (it provides rsync)"
    )

    ps1 = (REPO / "scripts" / "wsl-ci-distro.ps1").read_text()
    assert "base.yml" in ps1, "the distro script's printed next steps must include base.yml"


def test_lane_host_restricts_the_socket_proxy_at_the_firewall() -> None:
    """The bind address alone does NOT restrict 2375 on a mirrored-networking host.

    Every WSL distro shares one namespace there, and a lane container can route out
    through the docker bridge — so the dev distro, anything on Windows, and PR code
    running in a lane could all reach an unauthenticated container-create API and become
    root on the lane host. The restriction has to be enforced on the host (DOCKER-USER),
    not assumed from the Tailscale ACL. See ADR 0016 decision 7.
    """
    rules = [
        t.get("ansible.builtin.iptables")
        for t in _lane_host_tasks()
        if t.get("ansible.builtin.iptables")
    ]
    on_2375 = [r for r in rules if str(r.get("destination_port")) == "2375"]
    assert len(on_2375) >= 2, "expected an allow rule and a catch-all deny on 2375"

    allow = [r for r in on_2375 if r.get("jump") == "RETURN"]
    deny = [r for r in on_2375 if r.get("jump") == "DROP"]
    assert allow, "the controller must be permitted explicitly"
    assert deny, "everything else must be dropped — an allow-only rule restricts nothing"
    assert all(r.get("chain") == "DOCKER-USER" for r in on_2375), (
        "must live in DOCKER-USER: it is consulted before docker's own FORWARD rules "
        "and survives container restarts"
    )
    assert allow[0].get("source"), "the allow rule must be scoped to a source address"
    # Ordering is load-bearing: the allow is inserted, the deny appended.
    assert allow[0].get("action") == "insert"
    assert deny[0].get("action") == "append"


def test_lane_host_endpoint_and_ssh_port_agree_with_the_inventory() -> None:
    """Under mirrored networking the host is identified by (Windows name, port).

    The controller reaches the socket proxy at the Windows machine's tailnet name, and
    ansible reaches the distro at that same name on a non-default SSH port. If those
    drift apart, the controller talks to one machine and ansible provisions another.
    """
    config = yaml.safe_load(CONTROLLER_CONFIG.read_text())
    endpoint = config["hosts"]["powervaro-ci"]["docker_endpoint"]
    inventory = yaml.safe_load((REPO / "inventory" / "hosts.yml").read_text())
    entry = inventory["all"]["children"]["ci_hosts"]["hosts"]["powervaro-ci"]

    endpoint_host = endpoint.split("//", 1)[1].rsplit(":", 1)[0]
    assert endpoint_host == entry["ansible_host"], (
        f"controller talks to {endpoint_host}, ansible provisions {entry['ansible_host']}"
    )
    assert entry.get("ansible_port") not in (None, 22), (
        "port 22 belongs to the shared namespace; the CI distro needs its own port"
    )

    ps1 = (REPO / "scripts" / "wsl-ci-distro.ps1").read_text()
    assert f"Port {entry['ansible_port']}" in ps1, (
        "the distro script must configure sshd on the port the inventory expects"
    )
