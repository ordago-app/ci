"""The template's own contract, proven against a fixture.

Replaces test_compose_render.py, which rendered from a real operator's
personal/github-runners.yml at module scope and therefore could not be collected
in a repo that has no personal/ directory. A shared platform that shipped one
operator's host config to make its tests pass would be the neutrality decay
ci-controller/tests/test_no_operator_defaults.py exists to prevent.
"""

import re
from pathlib import Path

import jinja2
import yaml

TESTS = Path(__file__).resolve().parent
TEMPLATE = TESTS.parent / "compose.yml.j2"
HOST = "fixture-host"


def render() -> dict:
    pool = yaml.safe_load((TESTS / "fixtures" / "pool.yml").read_text())
    repos = yaml.safe_load((TESTS / "fixtures" / "repos.yml").read_text())
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE.parent)),
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )
    text = env.get_template(TEMPLATE.name).render(
        github_actions_runners=pool["github_actions_runners"],
        project_repos=repos["project_repos"],
        inventory_hostname=HOST,
    )
    return yaml.safe_load(text)


def test_each_enabled_runner_becomes_a_service_named_for_itself() -> None:
    # The compose service key is the runner's own name, not prefixed by the
    # host — the host only shows up in RUNNER_NAME and container_name.
    assert set(render()["services"]) == {"fixture-light", "fixture-heavy"}


def test_kvm_is_mounted_only_by_the_runner_that_declared_it() -> None:
    services = render()["services"]
    assert "/dev/kvm" in str(services["fixture-heavy"])
    assert "/dev/kvm" not in str(services["fixture-light"])


def test_a_disabled_runner_is_not_rendered() -> None:
    """The template, not ansible, is what skips a disabled runner."""
    pool = yaml.safe_load((TESTS / "fixtures" / "pool.yml").read_text())
    pool["github_actions_runners"]["runners"][0]["enabled"] = False
    repos = yaml.safe_load((TESTS / "fixtures" / "repos.yml").read_text())
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE.parent)),
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )
    rendered = yaml.safe_load(
        env.get_template(TEMPLATE.name).render(
            github_actions_runners=pool["github_actions_runners"],
            project_repos=repos["project_repos"],
            inventory_hostname=HOST,
        )
    )
    assert "fixture-light" not in rendered["services"]


def test_runner_version_arg_does_not_precede_the_android_layers():
    """Keep a runner-version bump from re-running the Android/maestro layers.

    `ci_lane_host` builds this image through `community.docker.docker_image`,
    which drives the daemon's CLASSIC builder. There, changing an ARG's default
    invalidates every layer BELOW the ARG -- not merely the ones referencing it.
    `ACTIONS_RUNNER_VERSION` therefore has to be declared under the Android
    layers, next to the runner download that is its only consumer.

    Declared at the top of the file (where it lived until GitHub deprecated
    2.335.1 and forced a bump) it re-ran ~10 GB of sdkmanager for a ~200 MB
    tarball, extending a total CI outage by hours.
    """
    dockerfile = (TESTS.parent / "Dockerfile").read_text()

    declarations = [
        m.start()
        for m in re.finditer(r"^ARG ACTIONS_RUNNER_VERSION", dockerfile, re.MULTILINE)
    ]
    assert len(declarations) == 1, (
        f"expected exactly one ACTIONS_RUNNER_VERSION declaration, got {len(declarations)}"
    )
    assert dockerfile.index("ARG WITH_ANDROID") < declarations[0], (
        "ARG ACTIONS_RUNNER_VERSION must be declared BELOW the Android SDK "
        "layers, or bumping the runner re-runs them on the classic builder"
    )
