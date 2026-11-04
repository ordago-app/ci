import os
import re
from pathlib import Path

import yaml

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SERVICES_PLAYBOOK = SERVICE_ROOT.parents[1] / "ansible" / "playbooks" / "services.yml"


def test_credential_helper_vendored_and_executable() -> None:
    # Regression: the worker runs `git fetch` against a PRIVATE repo over HTTPS
    # with no credentials, so fetch died with "could not read Username for
    # 'https://github.com'". It must authenticate as the reviewer App via the
    # same credential helper the agent containers use.
    helper = SERVICE_ROOT / "bin" / "git-credential-helper"
    assert helper.is_file(), "git-credential-helper must be vendored into the image build context"
    assert os.access(helper, os.X_OK), "git-credential-helper must be executable"
    body = helper.read_text()
    assert "internal/github-token?role=" in body, "helper must request a role-scoped token"
    # Behaviour lives in tests/test_agent_token_wrappers.py, which executes this
    # script; this is the build-context half — the file has to be here at all.
    assert "x-access-token" in body, "helper must return the App token as x-access-token"


def test_dockerfile_wires_credential_helper() -> None:
    dockerfile = (SERVICE_ROOT / "Dockerfile").read_text()
    assert "git-credential-helper" in dockerfile, "image must install the credential helper"
    assert "credential.helper" in dockerfile, "image must configure git credential.helper"


def test_compose_sets_reviewer_role() -> None:
    compose = (SERVICE_ROOT / "compose.yml").read_text()
    assert "AGENT_ROLE=reviewer" in compose, "compose must set AGENT_ROLE=reviewer for git auth"
    assert "ROUTER_INTERNAL_URL=" in compose, "compose must point the helper at the router"


def test_compose_runs_an_init_to_reap_zombies() -> None:
    # Regression: `python -m src.main` runs as PID 1 in the container. The
    # worker shells out to `git` (fetch/worktree/clean), and git's transient
    # helper children (git-remote-https, pack processes) get reparented to
    # PID 1 when their git parent exits. PID 1 here never reaps them, so they
    # linger as zombies on powerserver. `init: true` makes Docker run tini as
    # PID 1, which reaps exited children.
    compose = yaml.safe_load((SERVICE_ROOT / "compose.yml").read_text())
    svc = compose["services"]["github-review"]
    assert svc.get("init") is True, "compose must set init: true so PID 1 reaps git helper zombies"


# `test_project_clone_worktrees_dir_is_owned_by_the_operator` is NOT here, for
# the same reason: it asserted on `ansible/playbooks/services.yml`, which belongs
# to whoever deploys this, not to this repo. The other four tests in this file
# assert on THIS service's own Dockerfile and compose, so they stayed.


def test_compose_mounts_the_claude_key_read_only() -> None:
    compose = yaml.safe_load((SERVICE_ROOT / "compose.yml").read_text())
    svc = compose["services"]["github-review"]
    key_file = next(
        env.split("=", 1)[1]
        for env in svc["environment"]
        if env.startswith("CLAUDE_REVIEW_API_KEY_FILE=")
    )
    state_dir = str(Path(key_file).parent)
    assert f"{state_dir}:{state_dir}:ro" in svc["volumes"]
    assert not any("ANTHROPIC_API_KEY" in env for env in svc["environment"])


def test_claude_review_image_is_pinned_and_unprivileged() -> None:
    dockerfile = (SERVICE_ROOT / "claude-review" / "Dockerfile").read_text()
    assert re.search(r"@anthropic-ai/claude-code@\d+\.\d+\.\d+", dockerfile)
    assert re.search(r"^USER node$", dockerfile, re.MULTILINE)


def test_role_provisions_what_the_claude_provider_needs() -> None:
    tasks = yaml.safe_load(
        (SERVICE_ROOT.parents[1] / "roles/github_review/tasks/main.yml").read_text()
    )
    state_dirs = [t for t in tasks if "claude-review-state" in str(t.get("ansible.builtin.file"))]
    assert len(state_dirs) == 1
    assert state_dirs[0]["ansible.builtin.file"]["mode"] == "0700"
    images = [
        t["community.docker.docker_image"] for t in tasks if "community.docker.docker_image" in t
    ]
    assert images[0]["name"] == "homelab/claude-review"
    assert images[0]["build"]["path"].endswith("github-review/claude-review")
