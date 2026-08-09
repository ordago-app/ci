from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
COMPOSE = Path(__file__).resolve().parents[1] / "compose.yml"
SERVICES_PLAYBOOK = REPO / "ansible" / "playbooks" / "services.yml"
GROUP_VARS = REPO / "inventory" / "group_vars" / "all.yml"
LANE_HOST_COMPOSE = REPO / "services" / "ci-lane-host" / "compose.yml"
LANE_HOST_PLAYBOOK = REPO / "ansible" / "playbooks" / "ci-lane-host.yml"
CONTROLLER_CONFIG = REPO / "personal" / "ci-controller.yml"
LANE_AGENT_SCRIPT = REPO / "scripts" / "wsl-ci-sleep-inhibit.ps1"
DISTRO_SCRIPT = REPO / "scripts" / "wsl-ci-distro.ps1"


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


def test_lane_host_firewall_survives_a_reboot() -> None:
    """The restriction must be boot-restored, not a one-shot live iptables mutation.

    iptables rules are runtime state and docker restarts the `unless-stopped` proxy on
    boot, so a deploy-time-only rule leaves 2375 unfiltered after every WSL restart until
    someone re-runs the play — an unnoticed hole in the thing called a trust boundary.
    """
    by_dest = {}
    for task in _lane_host_tasks():
        copy = task.get("ansible.builtin.copy") or task.get("copy")
        if copy and "dest" in copy:
            by_dest[copy["dest"]] = copy.get("content", "")

    script = by_dest.get("/usr/local/sbin/ci-lane-firewall.sh")
    unit = by_dest.get("/etc/systemd/system/ci-lane-firewall.service")
    assert script is not None, "the rules need one definition, in a script"
    assert unit is not None, "the rules need a boot-time restore unit"

    # Both directions: an allow-only rule restricts nothing.
    assert "-j RETURN" in script, "the controller must be permitted explicitly"
    assert "-j DROP" in script, "everything else forwarded must be dropped"
    assert "DOCKER-USER" in script
    assert "-I DOCKER-USER 1" in script, "the allow must precede the catch-all deny"

    # Idempotent: re-running must not stack duplicate rules.
    assert script.count("-D DOCKER-USER") >= 2, "each rule must be deleted before it is added"

    # docker rebuilds its chains on start, so ordering after it is load-bearing.
    assert "After=docker.service" in unit
    assert "WantedBy=multi-user.target" in unit, "must actually be enabled at boot"

    enabled = [
        (t.get("ansible.builtin.systemd") or t.get("systemd") or {}) for t in _lane_host_tasks()
    ]
    assert any(
        u.get("name") == "ci-lane-firewall.service" and u.get("enabled") is True for u in enabled
    ), "the unit must be enabled, or it never runs at boot"


def test_windows_scripts_are_ascii_only() -> None:
    """Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI, not UTF-8.

    A non-ASCII byte inside a string then desynchronises the parser and the script dies
    several lines later with an error pointing nowhere near the real cause. This already
    cost one failed operator run, from an em-dash in a comment. No CI job executes these
    scripts, so this byte-level check is the only guard they get.
    """
    for script in (LANE_AGENT_SCRIPT, DISTRO_SCRIPT):
        raw = script.read_bytes()
        offending = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
        assert not offending, (
            f"{script.name} has non-ASCII bytes at offsets "
            f"{[i for i, _ in offending[:5]]} - PowerShell 5.1 will mis-parse it"
        )


def test_lane_agent_keeps_the_distro_running() -> None:
    """A WSL distro stops as soon as its last client process exits.

    systemd inside the distro does not prevent this, and nothing starts it at logon, so a
    fully provisioned lane host is simply absent a minute after the operator's last wsl
    command returns. The controller then correctly skips an unreachable host and the
    second lane host contributes nothing. Something must hold a process open.
    """
    script = LANE_AGENT_SCRIPT.read_text()

    assert "sleep" in script and "infinity" in script, (
        "the agent must hold a long-lived process inside the distro, or it stops"
    )
    assert "--exec" in script, "pin the process directly; this distro has interop disabled"
    assert "HasExited" in script, "a pin that is never re-established dies with the first crash"

    # The cmdlet's default ExecutionTimeLimit is 3 days: without an explicit override the
    # agent is killed on day 3 and the lane host vanishes until the next logon.
    assert "ExecutionTimeLimit" in script, (
        "the logon task must override the default 3-day execution time limit"
    )
    assert "[TimeSpan]::Zero" in script, "unlimited is the only correct limit for an agent"

    # -Install fails with a raw CIM "Access is denied" without elevation, which reads
    # as a bug in the script. It must say so itself, and the runbook must say so too.
    assert "Test-Elevated" in script, "-Install must check for elevation, not just fail"
    assert script.index("function Test-Elevated") < script.index("if ($Uninstall)"), (
        "the check must precede the -Uninstall branch, which returns early"
    )

    # Installing it is part of provisioning, not optional polish for `node` later.
    runbook = (REPO / "docs" / "runbook.md").read_text()
    section = runbook.split("## Second CI lane host")[1].split("\n## ")[0]
    assert LANE_AGENT_SCRIPT.name in section, (
        "the provisioning sequence must install the agent; without it there is no host"
    )
    assert "Administrator" in section, "the runbook must say -Install needs elevation"


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


def _daemon_json_template() -> str:
    """The literal content block of docker.yml's daemon.json task."""
    play = yaml.safe_load((SERVICES_PLAYBOOK.parent / "docker.yml").read_text())[0]
    for task in play["tasks"]:
        copy = task.get("ansible.builtin.copy") or task.get("copy") or {}
        if copy.get("dest") == "/etc/docker/daemon.json":
            return copy["content"]
    raise AssertionError("docker.yml no longer writes /etc/docker/daemon.json")


def _render(template: str, **ctx: object) -> str:
    import json as _json

    from jinja2 import Environment

    # trim_blocks=True mirrors ansible's own Templar. It is load-bearing: under it a
    # block tag ending a line swallows the newline after it.
    env = Environment(  # noqa: S701 - rendering a config file, not HTML
        autoescape=False, trim_blocks=True, keep_trailing_newline=True
    )
    env.filters["to_json"] = _json.dumps  # ansible's to_json is json.dumps
    return env.from_string(template).render(**ctx)


def test_daemon_json_template_uses_no_backslash_escapes() -> None:
    """Ansible does not interpret a `\\n` escape inside a Jinja string literal.

    It emits a literal backslash-n. That produced invalid JSON on powervaro-ci and dockerd
    refused to start. Plain jinja2 *does* interpret the escape, so the render test below
    passed while the real thing was broken -- this check is the one that generalises.
    """
    assert "\\n" not in _daemon_json_template(), (
        "use the _newline var (a YAML double-quoted scalar) instead of a Jinja escape"
    )


def test_daemon_json_is_unchanged_when_no_dns_is_configured() -> None:
    """Adding conditional DNS must not reformat the file on hosts that don't set it.

    This task notifies `restart docker`. Restarting the daemon on powerserver stops
    every service container and kills every live CI lane, so a cosmetic whitespace or
    key-order change here is an outage, not a diff. Pin the exact bytes.
    """
    rendered = _render(_daemon_json_template(), docker_daemon_dns=[], _newline="\n")
    assert rendered == (
        "{\n"
        '  "log-driver": "json-file",\n'
        '  "log-opts": {\n'
        '    "max-size": "10m",\n'
        '    "max-file": "3"\n'
        "  },\n"
        '  "default-address-pools": [\n'
        '    { "base": "172.30.0.0/16", "size": 24 }\n'
        "  ]\n"
        "}\n"
    )


def test_daemon_json_carries_dns_when_configured() -> None:
    """A WSL lane host's resolver is unreachable from the docker bridge.

    /etc/resolv.conf points at 10.255.255.254 (the WSL DNS proxy), docker copies that
    into every container, and nothing on the bridge can reach it — so the daemon pulls
    images fine while every container fails to resolve. Measured on powervaro-ci: it
    failed the runner image build at `apt-get update`.
    """
    import json as _json

    rendered = _render(
        _daemon_json_template(), docker_daemon_dns=["8.8.8.8", "1.1.1.1"], _newline="\n"
    )
    parsed = _json.loads(rendered)  # must still be valid JSON, or dockerd will not start
    assert parsed["dns"] == ["8.8.8.8", "1.1.1.1"]
    assert parsed["default-address-pools"] == [{"base": "172.30.0.0/16", "size": 24}], (
        "the conditional must not disturb the existing keys"
    )

    # And the lane hosts must actually set it, or the rendering above is dead surface.
    group_vars = yaml.safe_load((REPO / "inventory" / "group_vars" / "ci_hosts.yml").read_text())
    assert group_vars["docker_daemon_dns"], "ci_hosts must configure container DNS"
    defaults = yaml.safe_load(GROUP_VARS.read_text())
    assert defaults.get("docker_daemon_dns") == [], (
        "the default must be empty, or every other host's daemon.json changes"
    )
