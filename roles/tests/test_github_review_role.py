"""github_review role: one reviewer instance on any Docker host, from role variables.

The service's own compose contract is in services/github-review/tests/test_deploy_wiring.py;
this file holds the role to it."""

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
ROLE = REPO / "roles" / "github_review"
SERVICE = REPO / "services" / "github-review"
REQUIRED = (
    "github_review_instance",
    "github_review_root",
    "github_review_owner",
    "github_review_config",
)


def _tasks() -> list[dict]:
    return yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())


def _code(path: Path) -> str:
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.strip().startswith("#")
    )


def test_the_role_names_no_operator_and_borrows_nothing_from_one() -> None:
    """No operator path, user, machine or image: an instance must deploy on any
    Docker host from this role's variables alone."""
    for path in (ROLE / "tasks" / "main.yml", ROLE / "defaults" / "main.yml"):
        body = _code(path)
        for borrowed in (
            "personal_root",
            "services_root",
            "operator_user",
            "/opt/personal",
            "powerserver",
            "homelab",
            "claude-router",
            "codex-code",
        ):
            assert borrowed not in body, f"{path.name} names {borrowed!r}"


def test_what_has_no_safe_default_is_required_and_asserted() -> None:
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text()) or {}
    asserted = yaml.dump(
        [t["ansible.builtin.assert"] for t in _tasks() if "ansible.builtin.assert" in t]
    )
    for name in REQUIRED:
        assert name not in defaults, f"{name} has a default: one operator's value for everyone"
        assert name in asserted, f"{name} is required but never asserted"


def test_compose_gets_every_variable_it_requires() -> None:
    compose_vars = set(re.findall(r"\$\{(\w+):\?\}", (SERVICE / "compose.yml").read_text()))
    env_task = next(
        t["ansible.builtin.copy"]
        for t in _tasks()
        if str((t.get("ansible.builtin.copy") or {}).get("dest", "")).endswith("/app/.env")
    )
    rendered = set(re.findall(r"^(\w+)=", env_task["content"], re.MULTILINE))
    assert rendered == compose_vars


def test_secrets_are_rendered_privately_and_never_logged() -> None:
    for task in _tasks():
        copy = task.get("ansible.builtin.copy") or {}
        dest = str(copy.get("dest", ""))
        if "secrets" in dest:
            assert task.get("no_log") is True, f"{task['name']} would log a credential"
            assert copy.get("mode") == "0600"
            assert copy.get("owner") == "root"


def test_every_image_is_pulled_from_the_pinned_commit_and_none_is_built() -> None:
    """A host holds no copy of the code: the service and both sandboxes are the images
    this repository published at the commit the consumer pinned."""
    tasks = _tasks()
    assert not any(t.get("ansible.posix.synchronize") for t in tasks)
    pull = next(t for t in tasks if "community.docker.docker_image" in t)
    assert pull["community.docker.docker_image"]["source"] == "pull"
    assert pull["community.docker.docker_image"]["name"] == (
        "{{ ci_image_registry }}/{{ item }}:{{ ci_image_tag }}"
    )
    assert sorted(pull["loop"]) == ["github-review-claude", "github-review-codex"]

    up = next(t for t in tasks if t["name"] == "github-review stack up")
    assert up["community.docker.docker_compose_v2"]["build"] == "never"

    env = next(
        t["ansible.builtin.copy"]["content"]
        for t in tasks
        if str((t.get("ansible.builtin.copy") or {}).get("dest", "")).endswith("/app/.env")
    )
    for image in ("github-review", "github-review-claude", "github-review-codex"):
        assert f"/{image}:{{{{ ci_image_tag }}}}" in env
    assert "ci_image_tag" in yaml.dump(
        [t["ansible.builtin.assert"] for t in tasks if "ansible.builtin.assert" in t]
    )


def test_the_network_compose_joins_exists_before_compose_runs() -> None:
    tasks = _tasks()
    names = [t["name"] for t in tasks]
    network = next(i for i, t in enumerate(tasks) if "community.docker.docker_network" in t)
    up = names.index("github-review stack up")
    assert network < up
    assert (
        tasks[network]["community.docker.docker_network"]["name"] == "{{ github_review_network }}"
    )


def test_env_credentials_are_quoted_and_cannot_break_out_of_the_quotes() -> None:
    """Unquoted, a value with a space or `#` is cut short by Compose; quoted, a
    value holding the quote or a newline would end it and start a new variable.
    So the values are single-quoted, and the role refuses the two characters
    that could escape that."""
    tasks = _tasks()
    render = next(
        t
        for t in tasks
        if str(t.get("ansible.builtin.copy", {}).get("dest", "")).endswith("/secrets.env")
    )
    assert "{{ name }}='{{ value }}'" in render["ansible.builtin.copy"]["content"]

    guard = next(
        t
        for t in tasks
        if "ansible.builtin.assert" in t and "github_review_secrets" in str(t.get("loop", ""))
    )
    assert tasks.index(guard) < tasks.index(render), "the guard must run before the render"
    assert guard.get("no_log") is True
    checks = " ".join(guard["ansible.builtin.assert"]["that"])
    assert '"\'"' in checks and "'\\n'" in checks


def test_a_changed_config_or_credential_recreates_the_service() -> None:
    """agent-review.yml is a single-file bind mount that copy replaces, so a
    container left running keeps the old file: a config-only deploy reported
    success and the service ran the previous chain."""
    tasks = {t["name"]: t for t in _tasks()}
    registered = {
        tasks["This instance's config is in place"]["register"],
        tasks["Credentials named by env are in the service's env file"]["register"],
        tasks["Credentials named by file are in the secrets dir"]["register"],
    }
    recreate = tasks["github-review stack up"]["community.docker.docker_compose_v2"]["recreate"]
    assert "always" in recreate
    for name in registered:
        assert f"{name} is changed" in recreate, f"{name} does not force a recreate"


def _defaults() -> dict:
    return yaml.safe_load((ROLE / "defaults" / "main.yml").read_text()) or {}


def _env_content() -> str:
    return next(
        t["ansible.builtin.copy"]["content"]
        for t in _tasks()
        if str((t.get("ansible.builtin.copy") or {}).get("dest", "")).endswith("/app/.env")
    )


def test_state_and_clones_default_to_the_instance_root() -> None:
    """An instance that sets neither keeps them where every instance always had
    them, so an existing deploy renders the same .env and the same dirs."""
    defaults = _defaults()
    assert defaults["github_review_state_dir"] == "{{ github_review_root }}/state"
    assert defaults["github_review_projects_dir"] == "{{ github_review_root }}/projects"
    env = _env_content()
    assert "GITHUB_REVIEW_STATE_DIR={{ github_review_state_dir }}" in env
    assert "GITHUB_REVIEW_PROJECTS_DIR={{ github_review_projects_dir }}" in env
    seeded = {
        t["name"]: t for t in _tasks() if "github_review_codex_seeds" in str(t.get("loop", ""))
    }
    for task in seeded.values():
        body = yaml.dump(task)
        assert "github_review_root }}/state" not in body, f"{task['name']} ignores the state dir"
        assert "github_review_state_dir" in body


def test_a_dir_outside_the_root_is_checked_never_taken_over() -> None:
    """A state or clones dir outside github_review_root is one something else
    uses too: a reviewer's existing state, clones shared with other agents. The
    role creates and chowns only what lies inside its root; a dir outside it must
    already exist, and keeps its owner and mode."""
    tasks = _tasks()
    managed = next(
        t
        for t in tasks
        if "ansible.builtin.file" in t and "github_review_state_dir" in str(t.get("loop", ""))
    )
    assert "github_review_projects_dir" in str(managed["loop"])
    assert "github_review_root ~ '/'" in str(managed["when"])

    stat = next(
        t
        for t in tasks
        if "ansible.builtin.stat" in t and "github_review_state_dir" in str(t.get("loop", ""))
    )
    assert "not" in str(stat["when"]) and "github_review_root ~ '/'" in str(stat["when"])
    check = next(
        t
        for t in tasks
        if "ansible.builtin.assert" in t and stat["register"] in str(t.get("loop", ""))
    )
    assert "isdir" in " ".join(check["ansible.builtin.assert"]["that"])
    assert not any(
        "ansible.builtin.file" in t and stat["register"] in str(t.get("loop", "")) for t in tasks
    )


def test_codex_login_dirs_default_to_none_and_add_no_compose_file() -> None:
    assert _defaults()["github_review_codex_login_dirs"] == {}
    tasks = {t["name"]: t for t in _tasks()}
    absent = next(
        t
        for t in tasks.values()
        if str((t.get("ansible.builtin.file") or {}).get("path", "")).endswith(
            "/app/compose.override.yml"
        )
    )
    assert absent["ansible.builtin.file"]["state"] == "absent"
    assert "github_review_codex_login_dirs | length == 0" in str(absent["when"])


def test_a_codex_login_dir_is_mounted_where_the_service_looks_for_that_account() -> None:
    """A login kept outside the state dir (one other consumers on the host read)
    is mounted read-write at /var/lib/github-review/codex/<account>, the dir the
    README tells every codex account's `credential.file` to name. The service
    replaces auth.json through a sibling temp file, so the mount is the dir."""
    tasks = _tasks()
    override = next(
        t
        for t in tasks
        if str((t.get("ansible.builtin.copy") or {}).get("dest", "")).endswith(
            "/app/compose.override.yml"
        )
    )
    body = str(override["ansible.builtin.copy"]["content"])
    assert "/var/lib/github-review/codex/" in body and ":rw" in body
    assert "github_review_codex_login_dirs" in body
    assert "github_review_codex_login_dirs | length > 0" in str(override["when"])

    guard = next(
        t
        for t in tasks
        if "ansible.builtin.assert" in t
        and "github_review_codex_login_dirs" in t["ansible.builtin.assert"]["fail_msg"]
    )
    assert tasks.index(guard) < tasks.index(override)
    checks = " ".join(guard["ansible.builtin.assert"]["that"])
    assert "match('^/')" in checks, "a relative host path would mount from compose's cwd"
    assert "github_review_codex_seeds" in checks, "a seed would land under the mount, unseen"
    assert "isdir" in checks, "docker would create a missing dir as an empty root-owned one"


def test_a_codex_login_dir_is_never_chowned() -> None:
    """The dir is shared with whatever else reads that login; the service keeps
    the file's owner and mode when it refreshes (codex_auth.py), and so must the role."""
    for task in _tasks():
        file = task.get("ansible.builtin.file") or {}
        assert "github_review_codex_login_dirs" not in str(task.get("loop", "")) or not file, (
            f"{task['name']} manages a codex login dir"
        )
