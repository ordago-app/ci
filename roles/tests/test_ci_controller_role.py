"""ci_controller role: what the platform does, and what it must not assume.

The consumer's own tests cover the other half -- that this operator's pool config
agrees with the paths they hand the role. Those facts are only visible there.
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
ROLE = REPO / "roles" / "ci_controller"
TASKS_FILE = ROLE / "tasks" / "main.yml"


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS_FILE.read_text())


def test_the_role_hardcodes_no_operator_disk_layout() -> None:
    """A shared platform cannot assume one operator's mount points.

    This role hardcoded `/mnt/ci-ssd/...` -- one machine's NVMe -- for its work dirs,
    its three tool caches and the prune unit's ExecStart. On any other operator's
    machine those directories simply do not exist, so the role created them at the
    root of a disk that was never meant to hold them and lanes wrote to the wrong
    filesystem.

    This is the defect `services/ci-controller/tests/test_no_operator_defaults.py`
    exists to prevent, in a place that test does not reach: it guards the service's
    Python config, not the ansible roles beside it.
    """
    body = "\n".join(
        line for line in TASKS_FILE.read_text().splitlines() if not line.strip().startswith("#")
    )
    for path in ("/mnt/ci-ssd", "/opt/personal/github-actions"):
        assert path not in body, (
            f"the role hardcodes {path}, one operator's disk layout. Take it as a "
            "variable -- ci_controller_work_dirs / ci_controller_cache_root -- so a "
            "second operator's machine can use this role at all"
        )


def test_the_disk_layout_is_required_not_defaulted() -> None:
    """Deliberately no default, unlike the lane host's work dirs.

    A default here would be one operator's mount point wearing a neutral name: every
    other operator inherits it silently and finds out when lanes write to the wrong
    filesystem. Required-and-asserted fails immediately instead, naming what to set --
    the same call made for `default_host` when the dispatcher stopped defaulting
    placement to one machine.
    """
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text()) or {}
    for name in ("ci_controller_work_dirs", "ci_controller_cache_root"):
        assert name not in defaults, (
            f"{name} has a default. A disk path default is one operator's machine "
            "inherited by everyone else without them noticing"
        )

    asserted = yaml.dump(
        [t["ansible.builtin.assert"] for t in _tasks() if "ansible.builtin.assert" in t]
    )
    for name in ("ci_controller_work_dirs", "ci_controller_cache_root"):
        assert name in asserted, (
            f"{name} is required but never asserted; it fails late and obscurely"
        )


def test_the_prune_unit_covers_every_work_dir_it_was_given() -> None:
    """A systemd service + daily timer prune stale `*-work` dirs, backstopping the
    in-controller reap cleanup.

    The find command must cover EVERY work dir the operator configured. When those
    paths were hardcoded, adding a second work dir to the pool config left the new one
    growing forever with nothing reaping it -- and a full disk on a CI host presents as
    lanes deferring, not as a disk alert.
    """
    by_dest = {}
    for t in _tasks():
        copy = t.get("ansible.builtin.copy") or t.get("copy")
        if copy and "dest" in copy:
            by_dest[copy["dest"]] = copy.get("content", "")

    svc = by_dest.get("/etc/systemd/system/ci-controller-prune-workdirs.service")
    timer = by_dest.get("/etc/systemd/system/ci-controller-prune-workdirs.timer")
    assert svc is not None, "prune service unit must be deployed"
    assert timer is not None, "prune timer unit must be deployed"

    assert "ci_controller_work_dirs" in svc, (
        "the prune command names paths literally instead of expanding "
        "ci_controller_work_dirs; a work dir added later would never be pruned"
    )
    assert "-name '*-work'" in svc, "prune must target only lane work dirs"
    assert "-mtime +1" in svc, "prune must spare dirs younger than a day"
    assert "OnCalendar=daily" in timer

    enabled = any(
        (t.get("ansible.builtin.systemd_service") or t.get("ansible.builtin.systemd") or {}).get(
            "name"
        )
        == "ci-controller-prune-workdirs.timer"
        for t in _tasks()
    )
    assert enabled, "prune timer must be enabled/started via systemd"


def test_the_state_dir_follows_the_operators_personal_root() -> None:
    """The metrics state dir hardcoded /opt/personal/state, ignoring personal_root.

    An operator who set personal_root elsewhere got their metrics written to a path
    they never configured -- and, being a `file` task, silently created."""
    body = TASKS_FILE.read_text()
    assert "/opt/personal/state" not in body, (
        "the state dir ignores personal_root; an operator who moved personal_root "
        "gets state written somewhere they never configured"
    )


def test_an_unhealthy_dispatcher_is_actually_restarted() -> None:
    """The dispatcher borrows the fabric sidecar's network namespace, and docker does
    not restart a dependent when the namespace owner restarts. The result is silent and
    permanent: no DNS, every call failing, and a container that still reads `Up`.

    A healthcheck alone does not fix it -- compose marks the container unhealthy and
    takes no action -- so the role must also deploy something that reads the health
    state and acts. Shipping the probe without the actor would be a control that cannot
    fire, the defect retired in #3.
    """
    tasks = _tasks()
    by_dest = {}
    for t in tasks:
        copy = t.get("ansible.builtin.copy") or t.get("copy")
        if copy and "dest" in copy:
            by_dest[copy["dest"]] = copy.get("content", "")

    script = by_dest.get("/usr/local/sbin/ci-controller-healthcheck-watch")
    unit = by_dest.get("/etc/systemd/system/ci-controller-healthcheck.service")
    timer = by_dest.get("/etc/systemd/system/ci-controller-healthcheck.timer")
    assert script is not None, "the role must deploy a watcher script"
    assert unit is not None, "and a service unit to run it"
    assert timer is not None, "and a timer to run the service"

    assert "ExecStart=/usr/local/sbin/ci-controller-healthcheck-watch" in unit

    assert "health=unhealthy" in script, (
        "the watcher must filter on health state; the orphaned dispatcher stays "
        "`running`, so a liveness-based filter never matches it"
    )
    assert "docker restart ci-controller" in script
    assert "'name=^/ci-controller$'" in script, (
        "anchor the name filter: unanchored it also matches ci-controller-socket-proxy"
    )
    assert "echo" in script or "logger" in script, (
        "the watcher must log when it fires -- a silent auto-restart hides a recurring "
        "fault the same way the original failure hid itself"
    )

    assert "OnUnitActiveSec" in timer and "OnBootSec" in timer

    assert any(
        (t.get("ansible.builtin.systemd") or t.get("systemd") or {}).get("name")
        == "ci-controller-healthcheck.timer"
        for t in tasks
    ), "the timer must be enabled and started"


def test_the_compose_probe_can_see_a_dead_namespace() -> None:
    """The probe must exercise name resolution. `localhost` needs no resolver, so a
    probe of this dispatcher's own /status reaches its handler, which then calls the
    scheduler BY NAME and fails -- which is what makes it a resolution check.

    /healthz answers from the process itself and returned 200 throughout the 2026-08-26
    outage, when no repo was being polled at all. A liveness probe passes in exactly the
    broken state.
    """
    compose = yaml.safe_load((REPO / "services" / "ci-controller" / "compose.yml").read_text())
    hc = compose["services"]["ci-controller"].get("healthcheck")
    assert hc, "ci-controller must have a healthcheck"

    test = hc["test"]
    probe = " ".join(test) if isinstance(test, list) else str(test)
    assert "/status" in probe
    assert "/healthz" not in probe
    assert hc.get("retries", 0) >= 2
    assert "start_period" in hc


def test_the_stack_runs_the_published_image_of_the_pinned_commit() -> None:
    """Nothing is built on the host and no source is copied to it, so the host holds
    no copy of the code that could drift from the pinned commit."""
    tasks = _tasks()
    assert not any(t.get("ansible.posix.synchronize") for t in tasks)
    images = [
        t["community.docker.docker_image"] for t in tasks if t.get("community.docker.docker_image")
    ]
    assert images and all(img.get("source") == "pull" for img in images)

    (up,) = [
        t["community.docker.docker_compose_v2"]
        for t in tasks
        if t.get("community.docker.docker_compose_v2")
    ]
    assert up["build"] == "never"

    (env,) = [
        t["ansible.builtin.copy"]
        for t in tasks
        if str((t.get("ansible.builtin.copy") or {}).get("dest", "")).endswith("ci-controller/.env")
    ]
    assert env["content"].strip() == (
        "CI_CONTROLLER_IMAGE={{ ci_image_registry }}/ci-controller:{{ ci_image_tag }}"
    )

    compose = yaml.safe_load((REPO / "services" / "ci-controller" / "compose.yml").read_text())
    for name in ("ci-controller", "ci-scheduler"):
        service = compose["services"][name]
        assert "build" not in service, f"{name} must not build on the host"
        assert service["image"].startswith("${CI_CONTROLLER_IMAGE:?"), (
            f"{name} must run the image the role pinned, and fail loudly without it"
        )


def test_the_pin_and_every_runner_image_are_checked_before_anything_runs() -> None:
    """A pool config naming another commit's runner would spawn lanes this pin never
    chose; a tag that is not a full SHA would let every image move on its own."""
    tasks = _tasks()
    names = [t.get("name", "") for t in tasks]
    tag = names.index("ci_image_tag pins a commit of this repository")
    runner = names.index("Every runner_image in the pool config is this commit's")
    first_change = next(i for i, t in enumerate(tasks) if t.get("ansible.builtin.file"))
    assert tag < first_change and runner < first_change

    assert "^[0-9a-f]{40}$" in " ".join(tasks[tag]["ansible.builtin.assert"]["that"])
    condition = " ".join(tasks[runner]["ansible.builtin.assert"]["that"])
    assert "_runner ~ '-light'" in condition
