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


def test_the_sandbox_images_are_built_here_and_named_as_the_service_expects() -> None:
    build = next(t for t in _tasks() if "community.docker.docker_image" in t)
    image = build["community.docker.docker_image"]
    assert sorted(build["loop"]) == ["claude-review", "codex-review"]
    assert image["name"] == "ordago-ci/{{ item }}"
    assert image["tag"] == "{{ github_review_instance }}"
    assert image["build"]["path"] == "{{ github_review_root }}/app/{{ item }}"
    for item in build["loop"]:
        assert (SERVICE / item / "Dockerfile").is_file()
    compose = (SERVICE / "compose.yml").read_text()
    for item in build["loop"]:
        assert f"ordago-ci/{item}:${{GITHUB_REVIEW_INSTANCE:?}}" in compose


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
