import os
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]


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
    assert "x-access-token" in body, "helper must return the App token as x-access-token"


def test_dockerfile_wires_credential_helper() -> None:
    dockerfile = (SERVICE_ROOT / "Dockerfile").read_text()
    assert "git-credential-helper" in dockerfile, "image must install the credential helper"
    assert "credential.helper" in dockerfile, "image must configure git credential.helper"


def test_compose_sets_reviewer_role() -> None:
    compose = (SERVICE_ROOT / "compose.yml").read_text()
    assert "AGENT_ROLE=reviewer" in compose, "compose must set AGENT_ROLE=reviewer for git auth"
    assert "ROUTER_INTERNAL_URL=" in compose, "compose must point the helper at the router"
