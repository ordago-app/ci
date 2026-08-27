"""Behaviour tests for the `ci_lane_host` role.

Ported from homelab's `tests/test_deploy_wiring.py`, which guarded
`ansible/playbooks/ci-lane-host.yml` before that play's tasks moved here as
`roles/ci_lane_host/tasks/main.yml`. The tests that did not come with the move
now fail in homelab, because the tasks they parse no longer live there.

Only tests that assert facts about the ROLE's own behaviour are ported. A test
that read a consumer's `personal/` config asserts facts about one operator's
deployment and stays with that operator (commit 59338bc, "ci: leave the
operator-config tests with the operator") — `personal/` deliberately does not
exist in this repo, and shipping one operator's host config to make a test
pass here is exactly the neutrality decay
`services/ci-controller/tests/test_no_operator_defaults.py` exists to prevent.

Runs in the dependency-free harness (pytest + pyyaml only), matching
`template-tests` and `scripts-tests` in .github/workflows/pytest.yml.
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
LANE_HOST_TASKS_FILE = REPO / "roles" / "ci_lane_host" / "tasks" / "main.yml"
ROLE = REPO / "roles" / "ci_lane_host"


def _lane_host_tasks() -> list[dict]:
    """The role's bare task list.

    Unlike homelab's playbook, this file has no play wrapper (no `hosts:`,
    `vars:`, `[0]["tasks"]`) — `yaml.safe_load` returns the task list directly.
    """
    return yaml.safe_load(LANE_HOST_TASKS_FILE.read_text())


def test_lane_host_no_longer_computes_a_bind_address() -> None:
    """The bind-address machinery must not come back.

    It existed to guarantee 2375 was never published on 0.0.0.0: a `tailscale ip -4`
    read, a guard that stopped the play on an empty result, and a rendered .env. All
    of it was in service of publishing the port carefully. Not publishing it is a
    smaller thing to keep correct, so the whole apparatus is gone — and this asserts
    it stays gone, because reintroducing a published port would reopen the wildcard
    hazard the old guard existed to close.
    """
    tasks = _lane_host_tasks()
    assert not [
        t for t in tasks if "tailscale ip -4" in str(t.get("ansible.builtin.command") or "")
    ], "no bind address is needed: the proxy is not published on this host"

    rendered = "".join(str((t.get("ansible.builtin.copy") or {}).get("content", "")) for t in tasks)
    assert "CI_LANE_BIND_ADDR" not in rendered, "the bind-address .env must not be rendered"
    # A .env still exists, carrying the host's own name for the fabric sidecar. Not the
    # same thing: a hostname is not a secret, and an absent one stops the container,
    # where an absent bind address published an unauthenticated API on every interface.
    assert "CI_LANE_HOSTNAME" in rendered, (
        "the sidecar must register under the host's name; without it the node registers "
        "under a container ID and docker_endpoint resolves to nothing"
    )


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
    ), "ci_lane_host's tasks must build homelab/github-actions-runner locally"


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
    assert "/usr/local/bin/ci-lane-prune" in svc
    assert "CI_LANE_WORK_DIR=" in svc
    # Age-based eligibility is exactly what failed on powervaro-ci on 2026-08-09: a
    # work dir had to be a day old, and one day's burst filled the 40 GB VM. The
    # script keys on whether the lane's container still exists instead.
    assert "-mtime" not in svc
    assert "OnUnitActiveSec=15min" in timer


def test_lane_host_proxy_survives_a_reboot() -> None:
    """A boot unit must bring the stack back, and must not re-derive an address.

    Observed 2026-08-09 on powervaro-ci, when the proxy published on the tailnet
    address: after the VM was resized and restarted, docker's `restart:
    unless-stopped` raced tailscaled and lost —

        failed to bind host port 100.117.169.88:2375/tcp: cannot assign requested address

    docker backed off, gave up, and the host stayed silently dead until someone
    re-ran the play. The race is now structurally gone: the proxy publishes no host
    port, so there is no address to bind and nothing to wait for. The unit stays,
    because the iptables rules are still runtime state docker does not recreate, and
    because something must bring the stack up.

    The script asserting NO address derivation is the regression guard: bringing that
    back would mean the port is published again, and the race with it.
    """
    by_dest = {}
    for task in _lane_host_tasks():
        copy = task.get("ansible.builtin.copy") or task.get("copy")
        if copy and "dest" in copy:
            by_dest[copy["dest"]] = copy.get("content", "")

    unit = by_dest.get("/etc/systemd/system/ci-lane-proxy.service")
    assert unit is not None, "a boot unit must bring the stack back up"
    assert "docker.service" in unit, "compose needs docker up"

    script = by_dest.get("/usr/local/sbin/ci-lane-proxy-start.sh")
    assert script is not None, "the unit must run a script this role owns"
    assert "docker compose" in script, "the script must bring the stack up"
    assert "tailscale ip -4" not in script, (
        "no address is derived any more: the proxy is not published on this host, and "
        "re-deriving one would mean it is again"
    )
    assert "0.0.0.0" not in script, "never a wildcard listener"

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


def _lane_firewall_script() -> str:
    """The rendered-ish body of /usr/local/sbin/ci-lane-firewall.sh, jinja intact."""
    scripts = [
        t
        for t in _lane_host_tasks()
        if (t.get("ansible.builtin.copy") or t.get("copy") or {})
        .get("dest", "")
        .endswith("ci-lane-firewall.sh")
    ]
    assert scripts, "the lane-host firewall script task was renamed or removed"
    return (scripts[0].get("ansible.builtin.copy") or scripts[0]["copy"])["content"]


def test_runner_image_tag_and_android_flag_come_from_variables() -> None:
    """A consumer's scheduling check reads this host's image TAG to decide which job
    classes it may run -- `light` means "no Android SDK, never an emulator lane".

    That check lives in the consumer, because only there is the pool config visible.
    So the consumer must be able to SUPPLY the tag, not copy it. If these were
    literals here, a consumer could only hand-mirror them in its own config: its test
    would compare its mirror against itself, pass forever, and drift silently the
    moment this role's literal changed. Same argument as ci_lane_work_dir.
    """
    built = [
        t.get("community.docker.docker_image")
        for t in _lane_host_tasks()
        if t.get("community.docker.docker_image")
    ]
    runner = [img for img in built if img.get("name") == "homelab/github-actions-runner"]
    assert len(runner) == 1, f"expected one runner image build, found {len(runner)}"
    img = runner[0]

    assert "ci_lane_runner_image_tag" in str(img.get("tag")), (
        f"tag is {img.get('tag')!r}: it must come from ci_lane_runner_image_tag, or a "
        "consumer can only mirror a literal and its scheduling test guards nothing"
    )
    assert "ci_lane_runner_with_android" in str(img.get("build", {}).get("args", {})), (
        "WITH_ANDROID must derive from ci_lane_runner_with_android, or the tag a "
        "consumer supplies can silently disagree with what the image actually holds"
    )

    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    assert defaults["ci_lane_runner_image_tag"] == "light"
    assert defaults["ci_lane_runner_with_android"] is False


def test_a_contradictory_tag_and_android_flag_stop_the_play() -> None:
    """The consumer's inference "tag == light therefore no Android SDK" is only sound
    if the two cannot contradict each other. An image tagged `light` that carried the
    SDK -- or a `latest` that lacked it -- would make every consumer's scheduling
    check wrong while still passing. The role must refuse to build it."""
    asserts = [
        t
        for t in _lane_host_tasks()
        if t.get("ansible.builtin.assert")
        and "ci_lane_runner_image_tag" in str(t["ansible.builtin.assert"])
    ]
    assert len(asserts) == 1, "expected exactly one tag/Android-flag consistency assert"
    conditions = " ".join(asserts[0]["ansible.builtin.assert"]["that"])
    assert "light" in conditions and "latest" in conditions, (
        "the assert must pin BOTH named tags; guarding only one leaves the other "
        f"free to lie about what the image holds. Got: {conditions}"
    )


def test_the_role_installs_no_host_firewall_for_2375() -> None:
    """2375 is gated by the fabric tailnet ACL, enforced by tailscaled on the lane host
    itself, and by docker's inter-bridge isolation for containers. Not by iptables.

    This role used to write DOCKER-USER rules for 2375. They never sat on the traffic
    they claimed to filter, measured twice on a live host: a request from the
    dispatcher's fabric address matched no allow rule and reached /_ping anyway, and
    inserting an explicit RETURN for one probe container left it just as unable to
    reach the proxy.

    A control that cannot fire is worse than no control -- its presence reads as
    assurance. If someone reintroduces one here, it has to be because they proved it
    sits on the path, and this test is where they say so.
    """
    # Look for rules being ADDED. The retirement task necessarily names iptables and
    # DOCKER-USER to delete what it owns, so matching those words alone would flag the
    # cleanup as the thing it cleans up.
    body = "\n".join(
        line
        for line in LANE_HOST_TASKS_FILE.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    installs = [
        line.strip()
        for line in body.splitlines()
        if any(verb in line for verb in ("iptables -I", "iptables -A", "iptables-restore"))
        or ("-j DROP" in line or "-j RETURN" in line)
    ]
    assert not installs, (
        "the role writes iptables rules again. On the fabric these do not sit on the "
        f"proxy's path; prove otherwise before adding them back: {installs}"
    )

    # Strip fail_msg before checking: the preconditions assert explains in prose that
    # this variable is no longer read, and prose saying so must not read as a use.
    def _without_messages(node):
        if isinstance(node, dict):
            return {k: _without_messages(v) for k, v in node.items() if k != "fail_msg"}
        if isinstance(node, list):
            return [_without_messages(v) for v in node]
        return node

    assert "ci_lane_allowed_dispatchers" not in yaml.dump(_without_messages(_lane_host_tasks())), (
        "the role reads ci_lane_allowed_dispatchers again. Who may reach 2375 is the "
        "fabric ACL's decision; a second allowlist here would drift from it silently"
    )


def test_the_role_cleans_up_the_retired_firewall() -> None:
    """Dropping the tasks alone would leave every already-provisioned host with the unit
    still enabled and the rules reinstalling at each boot, forever.

    A removal that leaves its artifacts behind is worse than no removal: the operator
    believes it is gone, and `iptables -S` keeps saying otherwise.
    """
    tasks = _lane_host_tasks()
    dump = yaml.dump(tasks)

    assert "ci-lane-firewall.service" in dump, "nothing stops or removes the retired unit"
    removals = [
        t
        for t in tasks
        if (
            (t.get("ansible.builtin.file") or {}).get("state") == "absent"
            and "ci-lane-firewall" in str(t.get("ansible.builtin.file", {}).get("loop", ""))
        )
        or "ci-lane-firewall" in str(t.get("loop", ""))
    ]
    assert removals, "the retired unit and script are never deleted from disk"

    purge = [
        t for t in tasks if "iptables -D DOCKER-USER" in str(t.get("ansible.builtin.shell", ""))
    ]
    assert len(purge) == 1, (
        "exactly one task must delete the rules this role previously wrote; without it "
        "they survive on every host provisioned before the retirement"
    )
    assert 'index($0, "ci-lane-host:")' in str(purge[0]["ansible.builtin.shell"]), (
        "the purge must match the comment tag this role wrote, so it removes every rule "
        "it owned regardless of which source address that rule named"
    )
