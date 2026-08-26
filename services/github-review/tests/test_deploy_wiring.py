import os
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
