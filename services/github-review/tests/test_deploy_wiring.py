"""What the service's own Dockerfile, compose file and sandbox images promise.

The role that deploys them is tested in roles/tests/test_github_review_role.py."""

import os
import re
from pathlib import Path

import yaml

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    return yaml.safe_load((SERVICE_ROOT / "compose.yml").read_text())


def _service() -> dict:
    return _compose()["services"]["github-review"]


def test_credential_helper_vendored_and_executable() -> None:
    # Regression: the worker runs `git fetch` against a PRIVATE repo over HTTPS
    # with no credentials, so fetch died with "could not read Username for
    # 'https://github.com'". Behaviour: tests/test_git_worktree.py executes it.
    helper = SERVICE_ROOT / "bin" / "git-credential-helper"
    assert helper.is_file()
    assert os.access(helper, os.X_OK)


def test_dockerfile_wires_credential_helper() -> None:
    dockerfile = (SERVICE_ROOT / "Dockerfile").read_text()
    assert "credential.helper /usr/local/bin/git-credential-helper" in dockerfile
    # Without it the helper cannot tell which repo a request is for, and refuses.
    assert "credential.useHttpPath true" in dockerfile


def test_compose_runs_an_init_to_reap_zombies() -> None:
    # The worker shells out to git, whose transient helper children get
    # reparented to PID 1 and linger as zombies unless an init reaps them.
    assert _service().get("init") is True


def test_compose_names_no_operator_and_borrows_no_image() -> None:
    """Portability: everything host- or instance-specific is a required variable."""
    text = (SERVICE_ROOT / "compose.yml").read_text()
    for borrowed in ("/opt/personal", "homelab", "powerserver", "claude-router", "mcp-gateway"):
        assert borrowed not in text, f"compose.yml names {borrowed!r}"
    variables = set(re.findall(r"\$\{(\w+)", text))
    assert variables == {
        "GITHUB_REVIEW_INSTANCE",
        "GITHUB_REVIEW_PROJECTS_DIR",
        "GITHUB_REVIEW_STATE_DIR",
        "GITHUB_REVIEW_SECRETS_DIR",
        "GITHUB_REVIEW_NETWORK",
        "GITHUB_REVIEW_RUN_AS_UID",
        "GITHUB_REVIEW_RUN_AS_GID",
    }
    # `:?` on every one: unset stops compose with the name, never a default.
    assert set(re.findall(r"\$\{(\w+)\}|\$\{(\w+):[-=]", text)) == set()


def test_projects_are_mounted_at_their_host_path() -> None:
    """Review containers are started on the host daemon, which resolves their
    mounts by HOST path; and a worktree's .git names its clone by path."""
    svc = _service()
    assert "${GITHUB_REVIEW_PROJECTS_DIR:?}:${GITHUB_REVIEW_PROJECTS_DIR:?}:rw" in svc["volumes"]
    assert "PROJECTS_ROOT=${GITHUB_REVIEW_PROJECTS_DIR:?}" in svc["environment"]


def test_secrets_reach_the_service_without_landing_in_compose() -> None:
    svc = _service()
    assert svc["env_file"] == [{"path": "secrets.env", "required": True}]
    assert "${GITHUB_REVIEW_SECRETS_DIR:?}:/run/secrets/github-review:ro" in svc["volumes"]
    assert not any("KEY" in e or "TOKEN" in e for e in svc["environment"])


def test_the_sandbox_images_are_this_instances_own() -> None:
    env = dict(e.split("=", 1) for e in _service()["environment"])
    assert env["CLAUDE_REVIEW_IMAGE"] == "ordago-ci/claude-review:${GITHUB_REVIEW_INSTANCE:?}"
    assert env["CODEX_REVIEW_IMAGE"] == "ordago-ci/codex-review:${GITHUB_REVIEW_INSTANCE:?}"


def test_the_build_context_keeps_config_and_secrets_out_of_the_image() -> None:
    ignored = (SERVICE_ROOT / ".dockerignore").read_text().split()
    assert {"secrets.env", ".env", "agent-review.yml"} <= set(ignored)


def test_the_sandbox_images_are_pinned_and_unprivileged() -> None:
    claude = (SERVICE_ROOT / "claude-review" / "Dockerfile").read_text()
    codex = (SERVICE_ROOT / "codex-review" / "Dockerfile").read_text()
    assert re.search(r"@anthropic-ai/claude-code@\d+\.\d+\.\d+", claude)
    assert re.search(r"@openai/codex@\d+\.\d+\.\d+", codex)
    for dockerfile in (claude, codex):
        assert re.search(r"^USER node$", dockerfile, re.MULTILINE)
        installs = " ".join(line for line in dockerfile.splitlines() if not line.startswith("#"))
        assert " gh" not in installs and "credential.helper" not in installs
