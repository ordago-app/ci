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


def test_lane_host_firewall_survives_a_reboot() -> None:
    """The restriction must be boot-restored, not a one-shot live iptables mutation.

    iptables rules are runtime state and docker restarts the `unless-stopped` proxy on
    boot, so a deploy-time-only rule leaves 2375 unfiltered after every VM start until
    someone re-runs the role — an unnoticed hole in the thing called a trust boundary.
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
    #
    # Observed on powervaro-ci 2026-08-10: three identical RETURN and three identical
    # DROP rules after one boot plus two play runs, because iptables matches the FULL
    # rule spec and a `-D` that omitted the `-m comment` its `-A` added never matched.
    # That was first fixed by pairing every add with a spec-identical delete, which this
    # test asserted directly.
    #
    # The script now deletes by COMMENT and line number instead, which subsumes it: it
    # removes every rule the script owns whatever source address the rule names, so a
    # dispatcher that changed IP or left ci_lane_allowed_dispatchers cannot strand a
    # stale RETURN. A spec-identical delete could not express that, because the spec it
    # would have to match is one the script no longer knows.
    assert "iptables -D DOCKER-USER" in script, "rules must be removed before being re-added"
    assert "ALLOW_NOTE" in script and "DENY_NOTE" in script, (
        "both rule families need a stable comment — the comment is what deletion matches on"
    )
    assert "-s " not in script.split("iptables -L DOCKER-USER")[1].split("for ip in")[0], (
        "deletion must not name a source address: that is what left stale allows behind "
        "when a dispatcher's tailnet IP moved"
    )

    # docker rebuilds its chains on start, so ordering after it is load-bearing.
    assert "After=docker.service" in unit
    assert "WantedBy=multi-user.target" in unit, "must actually be enabled at boot"

    enabled = [
        (t.get("ansible.builtin.systemd") or t.get("systemd") or {}) for t in _lane_host_tasks()
    ]
    assert any(
        u.get("name") == "ci-lane-firewall.service" and u.get("enabled") is True for u in enabled
    ), "the unit must be enabled, or it never runs at boot"


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


def test_lane_host_firewall_deletes_by_comment_so_the_allowlist_can_shrink() -> None:
    """Deleting the rule it is about to add only converges while the list never changes.

    The previous script deleted `-s "$CONTROLLER"` — the very address it then inserted.
    A dispatcher whose tailnet IP moved, or one dropped from the allowlist, left its
    RETURN in DOCKER-USER with nothing able to delete it, and the lane host kept
    accepting container-create calls from an address no longer trusted. Matching on the
    comment removes every rule the script owns whatever source it names.
    """
    script = _lane_firewall_script()
    assert "--line-numbers" in script, (
        "deletion must match the rules this script owns by comment, not by "
        "reconstructing the spec it is about to add"
    )
    # By the namespace PREFIX, not either full note. Matching a full note deletes only
    # rules spelled the way the current script spells them, so renaming a note orphans
    # every rule written under the old name — observed 2026-08-24 on powervaro-ci, which
    # ended up carrying a stale third rule after the allow note was widened.
    assert "OWNER_TAG=" in script and "index($0, o)" in script, (
        "delete by the ci-lane-host: prefix so a renamed note cannot orphan old rules"
    )
    assert "sort -rn" in script, (
        "delete highest line number first — deleting by index shifts later indices down"
    )


def test_lane_host_firewall_allows_every_dispatcher_and_denies_the_rest() -> None:
    """Order is the boundary: each allow must precede the catch-all DROP."""
    script = _lane_firewall_script()
    assert "for ip in $ALLOWED" in script, (
        "the allow must loop over every resolved dispatcher, not a single address"
    )
    # Both rules are INSERTED at the head; neither is appended.
    #
    # An appended deny sits after everything already in DOCKER-USER, and docker seeds
    # that chain with an unconditional `-j RETURN` on some versions. Behind one, the
    # deny never fires and 2375 is open — while `iptables -L` still shows both rules,
    # in the right relative order, so the place you would look says it is fine. Order
    # in the SCRIPT is not order in the CHAIN: emitting the deny first and each allow
    # after it is what puts the allows in front of it, independent of the chain's tail.
    assert "-A DOCKER-USER" not in script, (
        "never append to DOCKER-USER — an appended rule lands behind docker's own "
        "tail RETURN, where it cannot fire"
    )
    deny_insert = script.index('-I DOCKER-USER 1 -p tcp --dport "$PORT" -j DROP')
    allow_insert = script.index('-s "$ip" -j RETURN')
    assert deny_insert < allow_insert, (
        "insert the deny first, then push each allow in front of it — reversing this "
        "leaves the deny ahead of the allows and the dispatcher is locked out"
    )


def test_lane_host_unresolvable_dispatcher_stops_the_play() -> None:
    """A dispatcher that does not resolve contributes nothing to the allowlist, so the
    role would go on to publish a proxy that DROPs it — a lane host invisible to its
    controller, which the dispatcher then has no way to distinguish from any other
    infra failure. Fail loudly instead."""
    tasks = _lane_host_tasks()
    lookups = [t for t in tasks if "getent ahostsv4" in str(t.get("ansible.builtin.command") or "")]
    assert lookups, "the role must resolve each allowed dispatcher"
    assert lookups[0].get("failed_when") is False, (
        "a failed lookup must fall through to the assert that names it, not abort raw"
    )

    guards = [
        t
        for t in tasks
        if (t.get("ansible.builtin.assert") or t.get("assert"))
        and "unresolved" in str(t.get("vars", ""))
    ]
    assert guards, "an unresolvable dispatcher must stop the play"

    # Counting is not enough, and this is not hypothetical: a double-escaped regex in
    # the extraction returned [None] on 2026-08-24 -- one entry per dispatcher, so the
    # count matched -- and rendered ALLOWED="None". That is zero allow rules behind a
    # catch-all DROP: the lane host silently unreachable by its own controller.
    conditions = str((guards[0].get("ansible.builtin.assert") or guards[0]["assert"])["that"])
    assert "select('none')" in conditions, (
        "the guard must reject a failed extraction, not just count the entries"
    )


def test_lane_host_read_only_lookups_run_under_check_mode() -> None:
    """`--check` must reach the trust boundary it exists to rehearse.

    The dispatcher lookup is a `command`, which ansible skips in check mode; skipped,
    it yields nothing and every dispatcher looks unresolvable, so the role stops
    before a single firewall rule. It reads nothing it could change, so it opts in.

    There used to be two such lookups. The bind-address read is gone with the
    published port; see test_lane_host_no_longer_computes_a_bind_address above.
    """
    reads = [
        t
        for t in _lane_host_tasks()
        if "getent ahostsv4" in str(t.get("ansible.builtin.command") or "")
    ]
    assert len(reads) == 1, f"expected the dispatcher lookup, found {len(reads)}"
    assert reads[0].get("check_mode") is False, (
        f"{reads[0].get('name')!r} must set `check_mode: false`, or --check stops the "
        "role before it reaches a single firewall rule"
    )


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
