import re
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
    The real value is rendered into .env by ansible, which asks the host's own
    tailscaled for it. The bind address is necessary but NOT sufficient; see
    test_lane_host_restricts_the_socket_proxy_at_the_firewall.
    """
    (published,) = _lane_host_compose()["services"]["docker-socket-proxy"]["ports"]
    host_part = str(published).rsplit(":", 2)[0]
    assert host_part not in ("", "0.0.0.0", "::", "[::]"), (
        f"lane-host proxy publishes {published!r} without a specific bind address"
    )
    assert "CI_LANE_BIND_ADDR" in host_part, "bind address must come from the ansible-rendered .env"


def test_lane_host_bind_address_comes_from_tailscaled_and_stops_the_play_when_empty() -> None:
    """An empty bind address must abort, because the fallback is 0.0.0.0.

    `.env` is rendered unconditionally, so a discovery step that yields "" produces
    `CI_LANE_BIND_ADDR=` — and docker reads an empty host part as every interface,
    publishing an unauthenticated container-create API to every network the host can
    see. The guard, not the discovery, is what makes this safe; pin both.
    """
    tasks = _lane_host_tasks()

    discovery = [
        t for t in tasks if "tailscale ip -4" in str(t.get("ansible.builtin.command") or "")
    ]
    assert discovery, (
        "the bind address must be asked of the host's own tailscaled — a lane host is "
        "its own tailnet node now (ADR 0017)"
    )
    # Without this the play dies on a host that has no tailscale binary, before ever
    # reaching the guard that explains why.
    assert discovery[0].get("failed_when") is False, (
        "a missing/failed tailscale must fall through to the guard, not abort raw"
    )

    guards = [
        t
        for t in tasks
        if (t.get("ansible.builtin.fail") or t.get("fail"))
        and "ci_lane_bind_addr" in str(t.get("when", ""))
    ]
    assert guards, "an empty bind address must stop the play"
    assert "length == 0" in str(guards[0]["when"]), (
        f"the guard must trip on an empty address, not on {guards[0]['when']!r}"
    )

    # Ordering is the whole point: a guard after the render has already written 0.0.0.0.
    def index_of(pred) -> int:
        return next(i for i, t in enumerate(tasks) if pred(t))

    render = index_of(
        lambda t: (
            (t.get("ansible.builtin.copy") or t.get("copy") or {})
            .get("dest", "")
            .endswith("ci-lane-host/.env")
        )
    )
    assert tasks.index(guards[0]) < render, "the guard must precede rendering .env"


def test_lane_host_budgets_fit_the_vm_it_actually_runs_on() -> None:
    """A lane host must not inherit powerserver's RAM budget.

    The top-level ram_budget_mb is powerserver's 11 GB; the lane VM has 6 GB, capped by
    `--disk`/`--memory` in `make ci-lane-up`. Inheriting made `ci-status` report a budget
    the box does not have — harmless only while max_concurrent_lanes bound first, and a
    real over-admission trap the moment the lane cap rose. So the host must name its own
    budget, and the worst case its gates permit must fit inside it.

    Read from the Makefile rather than restated here: a VM resize that forgot this file
    should fail loudly instead of silently over-admitting.
    """
    config = yaml.safe_load(CONTROLLER_CONFIG.read_text())
    lane = config["hosts"]["powervaro-ci"]

    assert "ram_budget_mb" in lane, (
        "a lane host must state its own ram_budget_mb, not inherit powerserver's"
    )
    assert lane["ram_budget_mb"] < config["ram_budget_mb"], (
        "the lane VM is smaller than powerserver; its budget must be too"
    )

    makefile = (REPO / "Makefile").read_text()
    (vm_mem,) = re.findall(r"^CI_LANE_MEM \?= (\d+)G", makefile, re.M)
    vm_mem_mb = int(vm_mem) * 1024
    assert lane["ram_budget_mb"] < vm_mem_mb, (
        f"budget {lane['ram_budget_mb']} MB does not fit the VM's {vm_mem_mb} MB"
    )

    # The lane cap must bind before RAM does — that is what makes the cap the operator's
    # dial and keeps admissions predictable.
    worst_case = lane["max_concurrent_lanes"] * min(
        c["ram_mb"] for name, c in config["classes"].items() if name in lane["allowed_classes"]
    )
    assert worst_case <= lane["ram_budget_mb"], (
        f"{lane['max_concurrent_lanes']} lanes reserve {worst_case} MB, over the "
        f"{lane['ram_budget_mb']} MB budget — RAM would bind before the lane cap"
    )


def test_lane_host_image_tag_and_runner_image_agree() -> None:
    """The controller can only RUN an image, never build or pull one.

    The lane-host proxy denies IMAGES and BUILD, so if the tag ansible builds and the
    tag the controller names ever drift apart, every admission to that host 404s --
    and it reads as a controller bug rather than a naming mismatch. Pin them together.
    """
    build = [
        (t.get("community.docker.docker_image") or {})
        for t in _lane_host_tasks()
        if t.get("community.docker.docker_image")
    ]
    assert build, "the play must build the runner image locally"
    built = f"{build[0]['name']}:{build[0]['tag']}"

    config = yaml.safe_load(CONTROLLER_CONFIG.read_text())
    lane = config["hosts"]["powervaro-ci"]
    assert lane["runner_image"] == built, (
        f"ansible builds {built} but the controller would run {lane['runner_image']} on this host"
    )


def test_a_host_without_the_android_sdk_may_not_schedule_jobs_that_need_it() -> None:
    """The light image carries no Android SDK, so its host must not allow such classes.

    This is the invariant that makes the smaller image safe. `light` sets
    SKIP_ANDROID_SDK=1 so the entrypoint never looks for the seed; any class with
    needs_android_sdk would look, find nothing, and fail every job on the host.
    """
    play_build = [
        (t.get("community.docker.docker_image") or {}).get("build", {})
        for t in _lane_host_tasks()
        if t.get("community.docker.docker_image")
    ]
    built_without_android = str(play_build[0].get("args", {}).get("WITH_ANDROID")) == "0"
    if not built_without_android:
        return  # the host builds the full image; nothing to constrain

    config = yaml.safe_load(CONTROLLER_CONFIG.read_text())
    classes = config["classes"]
    for class_name in config["hosts"]["powervaro-ci"]["allowed_classes"]:
        assert not classes[class_name].get("needs_android_sdk", False), (
            f"host builds the image with WITH_ANDROID=0 but allows class "
            f"'{class_name}', which needs the Android SDK"
        )


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


def test_lane_host_proxy_survives_a_reboot() -> None:
    """The proxy publishes on the TAILNET address, which does not exist at boot.

    Observed 2026-08-09: after the VM was resized and restarted, docker's
    `restart: unless-stopped` raced tailscaled and lost —

        failed to bind host port 100.117.169.88:2375/tcp:
        cannot assign requested address

    docker backs off, gives up, and the host stays silently dead until someone
    re-runs this play. `ci-lane-firewall.service` already exists for the same class
    of reason (rules lost on boot); the proxy needs its own unit for the same
    reason, ordered after tailscaled rather than merely after docker.
    """
    by_dest = {}
    for task in _lane_host_tasks():
        copy = task.get("ansible.builtin.copy") or task.get("copy")
        if copy and "dest" in copy:
            by_dest[copy["dest"]] = copy.get("content", "")

    unit = by_dest.get("/etc/systemd/system/ci-lane-proxy.service")
    assert unit is not None, (
        "a boot unit must (re)start the proxy once the tailnet address exists — "
        "docker's restart policy alone loses the race and gives up"
    )
    # After docker is not enough: docker is up long before tailscaled has an address.
    assert "tailscaled.service" in unit, "the unit must be ordered after tailscaled"
    assert "docker.service" in unit, "compose needs docker up"

    script = by_dest.get("/usr/local/sbin/ci-lane-proxy-start.sh")
    assert script is not None, "the unit must run a script this play owns"
    # The whole hazard is a bind address that is absent or stale, so the script
    # re-derives it rather than trusting the .env rendered at deploy time.
    assert "tailscale ip -4" in script, (
        "the address must be re-derived at boot — the deploy-time .env goes stale "
        "if the node's tailnet address ever changes"
    )
    # Same invariant as the play's own guard: never fall back to every interface.
    assert "0.0.0.0" not in script, (
        "no wildcard fallback: an absent tailnet address must mean NO listener, "
        "never an unauthenticated container-create API on every interface"
    )
    assert "exit 1" in script, "an address that never arrives must fail, not proceed"

    enabled = [
        t
        for t in _lane_host_tasks()
        if (t.get("ansible.builtin.systemd") or t.get("systemd") or {}).get("name")
        == "ci-lane-proxy.service"
    ]
    assert (
        enabled
        and enabled[0][
            next(k for k in ("ansible.builtin.systemd", "systemd") if k in enabled[0])
        ].get("enabled")
        is True
    ), "the unit is useless unless enabled for the next boot"


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
    lane VM — and silently skips the base hardening too. Pin both the requirement and
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

    # The VM has to exist before any play can reach it, so the sequence starts there.
    assert "make ci-lane-up" in section, "the provisioning sequence must start by creating the VM"
    assert section.index("make ci-lane-up") < section.index("base.yml"), (
        "the VM must exist before base.yml can run against it"
    )


def test_lane_host_firewall_survives_a_reboot() -> None:
    """The restriction must be boot-restored, not a one-shot live iptables mutation.

    iptables rules are runtime state and docker restarts the `unless-stopped` proxy on
    boot, so a deploy-time-only rule leaves 2375 unfiltered after every VM start until
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


def test_lane_host_endpoint_and_ssh_port_agree_with_the_inventory() -> None:
    """A lane host is one machine reached two ways, so both ways must name it.

    The controller reaches the socket proxy at the host's tailnet name and ansible
    reaches sshd at the same name. If those drift apart, the controller talks to one
    machine and ansible provisions another.

    The port assertion is inverted from what it was under the WSL predecessor. That host
    shared the Windows machine's network namespace, so port 2222 was the only thing
    telling it apart from the desktop. The VM has its own stack and its own tailnet
    identity, so 22 is correct — and a non-default port here would mean someone
    reintroduced the shared-namespace hack. See ADR 0017.
    """
    config = yaml.safe_load(CONTROLLER_CONFIG.read_text())
    endpoint = config["hosts"]["powervaro-ci"]["docker_endpoint"]
    inventory = yaml.safe_load((REPO / "inventory" / "hosts.yml").read_text())
    entry = inventory["all"]["children"]["ci_hosts"]["hosts"]["powervaro-ci"]

    endpoint_host = endpoint.split("//", 1)[1].rsplit(":", 1)[0]
    assert endpoint_host == entry["ansible_host"], (
        f"controller talks to {endpoint_host}, ansible provisions {entry['ansible_host']}"
    )
    assert entry.get("ansible_port", 22) == 22, (
        "a lane VM is its own tailnet node, so sshd is on 22; a custom port means the "
        "shared-namespace workaround came back"
    )
    assert entry["ansible_host"].startswith("powervaro-ci."), (
        "the VM has its own MagicDNS name; pointing at the Windows host's name would "
        "provision the operator's desktop instead"
    )


def _daemon_json_template() -> str:
    """The literal content block of docker.yml's daemon.json task."""
    play = yaml.safe_load((SERVICES_PLAYBOOK.parent / "docker.yml").read_text())[0]
    for task in play["tasks"]:
        copy = task.get("ansible.builtin.copy") or task.get("copy") or {}
        if copy.get("dest") == "/etc/docker/daemon.json":
            return copy["content"]
    raise AssertionError("docker.yml no longer writes /etc/docker/daemon.json")


def test_daemon_json_is_a_byte_exact_literal() -> None:
    """Rewriting this file notifies `restart docker`, which is an outage.

    On powerserver a docker restart stops every service container and kills every
    live CI lane, so a cosmetic reformat here is not a diff -- it is downtime. The
    content must therefore stay a plain literal with no templating: pin it byte for
    byte, so any edit has to be deliberate.

    This replaces three tests that guarded a conditional `dns` key. That key existed
    only for the WSL lane host's unreachable resolver (#116/#117) and lost its last
    consumer when the host became a VM (ADR 0017); rendering with no consumer is the
    dead surface those tests were themselves written to forbid.
    """
    expected = (
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
    assert _daemon_json_template() == expected

    # No Jinja at all: ansible does not interpret a `\n` escape inside a string
    # literal, and a block tag ending a line swallows the newline after it under
    # trim_blocks. Both produced real outages here. A literal cannot do either.
    assert "{{" not in _daemon_json_template()
    assert "{%" not in _daemon_json_template()


def test_ci_status_script_reports_each_host_and_a_correct_total() -> None:
    """`make ci-status` must not read as an over-admission on a multi-host pool.

    The fleet-wide `lanes_running` counts every host but `max_lanes` is one host's
    ceiling, so printing them together showed "6 / 5" while both hosts were within
    their limits. Anyone diagnosing capacity would chase a bug that isn't there.
    Executes the real script against a synthetic payload — asserting on its source
    text would not catch a formatting change that drops a host.
    """
    import io
    import json
    import urllib.request
    from contextlib import redirect_stdout
    from unittest.mock import patch

    payload = {
        "hosts": {
            "powerserver": {
                "lanes": 4,
                "max_lanes": 5,
                "ram_mb": 9000,
                "budget_ram_mb": 11000,
                "healthy": True,
            },
            "powervaro-ci": {
                "lanes": 2,
                "max_lanes": 2,
                "ram_mb": 1400,
                "budget_ram_mb": 11000,
                "healthy": False,
            },
        },
        "lanes_running": 6,
        "max_lanes": 5,
        "ledger_ram_mb": 10400,
        "budget_ram_mb": 11000,
        "disk_gb": {"ssd": {"used": 30, "budget": 300}},
        # /status always stamps state (and idle_seconds for a lane that has not yet
        # been handed a job) on every running entry — the script keys off both.
        "running": [
            {"class": "node", "state": "busy", "idle_seconds": None},
            {"class": "light", "state": "booting", "idle_seconds": 25},
        ],
        "deferred": [{"reason": "budget_full"}],
        "admission_mode": "reservation_plus_guard",
    }
    source = (REPO / "scripts" / "ci_status.py").read_text()
    buf = io.StringIO()
    with (
        patch.object(urllib.request, "urlopen", return_value=io.StringIO(json.dumps(payload))),
        redirect_stdout(buf),
    ):
        exec(compile(source, "ci_status.py", "exec"), {"__name__": "__main__"})
    out = buf.getvalue()

    assert "powerserver" in out and "powervaro-ci" in out, "every host must be listed"
    assert "4/5" in out, "powerserver's own lanes against its own ceiling"
    assert "2/2" in out, "the lane host's own lanes against its own ceiling"
    assert "6/7" in out, "the total must sum the per-host ceilings, not reuse one host's"
    assert "6/5" not in out, "the misleading aggregate-vs-one-ceiling line must be gone"
    # An unhealthy host receives no work, which is indistinguishable from an idle pool.
    assert "UNHEALTHY" in out, "an unhealthy host must be called out, not silently idle"
    # A booting lane holds its full reserve but is running nothing; counting it under
    # `running:` is what made a warm pool read as a busy one.
    assert "busy:      {'node': 1}" in out, "only busy lanes belong under the busy label"
    assert "booting:   1 (light:25s)" in out, "a booting lane must be reported as booting"


def test_jobs_reaching_in_cluster_services_are_pinned_to_the_host_running_them() -> None:
    """A lane host creates the `homelab` network but nothing runs on it.

    So a job that curls a service by container name (`http://github-review:8000`)
    resolves only on the host where that container lives. Every lane used to run on
    powerserver, so the coupling existed but was never exercised; with a second host
    the job fails with "could not reach github-review" whenever it lands there.

    `allowed_classes` is the only per-host placement control, so such a job must map to
    a class no lane host allows. Reimplements `class_for`'s first-match-in-runs-on-order
    semantics, because that ordering is exactly how this silently regresses: adding the
    generic label alongside the specific one makes the generic one win.
    """
    import re

    config = yaml.safe_load(CONTROLLER_CONFIG.read_text())
    default_host = config["default_host"]
    all_classes = set(config["classes"])

    # Every class any NON-default host would accept.
    off_default: set[str] = set()
    for name, host in config["hosts"].items():
        if name == default_host:
            continue
        allowed = host.get("allowed_classes")
        off_default |= all_classes if allowed is None else set(allowed)

    label_class = next(r["label_class"] for r in config["repos"] if r["repo"].endswith("/homelab"))
    # A dotted host is a real DNS name; a bare one is a docker-network alias.
    in_cluster = re.compile(r"http://(?!localhost|127\.0\.0\.1)[a-z0-9-]+:\d+")

    checked = 0
    for workflow in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        text = workflow.read_text()
        if not in_cluster.search(text):
            continue
        for job_name, job in yaml.safe_load(text)["jobs"].items():
            runs_on = job.get("runs-on")
            if not isinstance(runs_on, list):
                continue
            resolved = next((label_class[x] for x in runs_on if x in label_class), None)
            assert resolved is not None, (
                f"{workflow.name}:{job_name} reaches an in-cluster service but its "
                f"runs-on {runs_on} maps to no class"
            )
            assert resolved not in off_default, (
                f"{workflow.name}:{job_name} reaches an in-cluster service but resolves "
                f"to class {resolved!r}, which a lane host accepts — it will fail there"
            )
            checked += 1

    assert checked, "no workflow matched; the guard would pass vacuously"


def test_every_controller_label_is_registered_with_actionlint() -> None:
    """actionlint rejects an unknown self-hosted label, failing the whole workflow lint.

    A label added to the controller's map but not here fails CI with a message about
    runs-on rather than about the controller, so the two lists must be kept together.
    Cheap to assert; it cost a full CI round-trip to learn the hard way.
    """
    config = yaml.safe_load(CONTROLLER_CONFIG.read_text())
    homelab = next(r for r in config["repos"] if r["repo"].endswith("/homelab"))
    known = set(
        yaml.safe_load((REPO / ".github" / "actionlint.yaml").read_text())["self-hosted-runner"][
            "labels"
        ]
    )
    missing = set(homelab["label_class"]) - known
    assert not missing, f"labels used by homelab jobs but unknown to actionlint: {sorted(missing)}"


def test_ci_status_script_names_the_skew_against_an_older_controller() -> None:
    """The script is piped from a checkout into whatever controller is deployed, so
    between a merge and the next deploy it runs against a payload older than itself.

    It must say that, not crash and not paper over it. A `state` default would report
    every lane as busy — wrong, and indistinguishable from a genuinely busy pool, which
    is the reading this whole booting/busy split exists to prevent.
    """
    import io
    import json
    import urllib.request
    from contextlib import redirect_stdout
    from unittest.mock import patch

    payload = {
        "hosts": {
            "powerserver": {
                "lanes": 2,
                "max_lanes": 5,
                "ram_mb": 1400,
                "budget_ram_mb": 11000,
                "healthy": True,
            }
        },
        "lanes_running": 2,
        "max_lanes": 5,
        "ledger_ram_mb": 1400,
        "budget_ram_mb": 11000,
        "disk_gb": {"ssd": {"used": 10, "budget": 300}},
        # A controller from before the busy/booting split: no `state`, no `idle_seconds`.
        "running": [{"class": "light"}, {"class": "light"}],
        "deferred": [],
        "admission_mode": "reservation_plus_guard",
    }
    source = (REPO / "scripts" / "ci_status.py").read_text()
    buf = io.StringIO()
    with (
        patch.object(urllib.request, "urlopen", return_value=io.StringIO(json.dumps(payload))),
        redirect_stdout(buf),
    ):
        exec(compile(source, "ci_status.py", "exec"), {"__name__": "__main__"})
    out = buf.getvalue()

    assert "predates" in out, "the skew must be named, not silently absorbed"
    assert "lanes:     {'light': 2}" in out, "what the old payload does support is still shown"
    assert "busy:" not in out, "claiming a busy count the payload cannot support is the bug"
    assert "powerserver" in out and "2/5" in out, "the rest of the report must still print"
