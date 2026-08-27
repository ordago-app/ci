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
