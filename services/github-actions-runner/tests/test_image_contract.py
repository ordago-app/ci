"""The image toolbox contract (README, "What the image has"), checked statically:
no CI job builds this image, so the Dockerfile text is the only thing to check."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


# ── The image's toolbox is a contract (README "What the image has") ──────────
# No CI job builds this image, so a static check is the only guard available.
# The contract exists because a hosted->self-hosted move changes a job's
# TOOLCHAIN, not just its price, and nothing type-checks a shell script's
# assumptions: the failure is exit 127 at run time, per repo, forever.
# ordago-apps 2026-09-01 is the worked example -- an announce poller that could
# no longer switch itself off, and a release-recovery job broken in silence.

# Every package the README promises. `nodejs` arrives via the NodeSource layer
# rather than the base apt block, so it is matched separately below.
CONTRACT_APT_PACKAGES = {
    "gh",
    "jq",
    "curl",
    "git",
    "unzip",
    "zip",
    "openssl",
    "xz-utils",
}


def _apt_install_chunks(dockerfile: str) -> list[tuple[int, str]]:
    """(offset, text) for each `apt-get install` invocation, offset into the file."""
    chunks: list[tuple[int, str]] = []
    cursor = 0
    needle = "apt-get install"
    while (found := dockerfile.find(needle, cursor)) != -1:
        cursor = found + len(needle)
        chunks.append((found, dockerfile[cursor:]))
    return chunks


def _packages_in_chunk(chunk: str) -> set[str]:
    packages: set[str] = set()
    for raw in chunk.replace("\\\n", " ").split():
        token = raw.strip()
        if token in {"-y", "--no-install-recommends"}:
            continue
        if token.startswith("-") or token == "&&":
            break
        packages.add(token)
    return packages


def _installed_apt_packages(dockerfile: str) -> set[str]:
    """Package names this Dockerfile actually passes to `apt-get install`.

    Parsed rather than substring-matched on purpose: `"gh" in dockerfile` is
    true of the PRE-change file (it matches `github`, `openjdk`, `libgtk`), so a
    substring assertion passes on revert and guards nothing.
    """
    packages: set[str] = set()
    for _, chunk in _apt_install_chunks(dockerfile):
        packages |= _packages_in_chunk(chunk)
    return packages


def test_image_installs_the_whole_contract_toolbox():
    dockerfile = (REPO_ROOT / "services/github-actions-runner/Dockerfile").read_text()
    installed = _installed_apt_packages(dockerfile)

    missing = CONTRACT_APT_PACKAGES - installed
    assert not missing, (
        f"the README promises these to every repo on the pool, and they are not installed: "
        f"{sorted(missing)}. Removing one is a breaking change for every workflow that "
        f"assumes ubuntu-latest's toolbox, not a size optimisation."
    )
    assert "nodejs" in installed, "the NodeSource layer must still install nodejs"


def test_contract_tools_are_installed_unconditionally():
    """`gh` must not drift inside a `WITH_ANDROID` guard.

    WITH_ANDROID=0 builds the `:light` image for lane hosts that can never run an
    emulator (entrypoint.sh deploys it for real). If a future edit moves the
    install into one of the `test "${WITH_ANDROID}" = "1" || exit 0` RUNs, every
    light host silently loses the tool again while a naive grep test still passes.
    """
    dockerfile = (REPO_ROOT / "services/github-actions-runner/Dockerfile").read_text()

    guarded_regions = []
    for chunk in dockerfile.split('RUN test "${WITH_ANDROID}" = "1" || exit 0;')[1:]:
        # A guarded RUN ends at the next top-level instruction.
        end = len(chunk)
        for line_start in ("\nRUN ", "\nARG ", "\nENV ", "\nCOPY ", "\nWORKDIR "):
            found = chunk.find(line_start)
            if found != -1:
                end = min(end, found)
        guarded_regions.append(chunk[:end])

    for tool in ("gh", "jq"):
        for region in guarded_regions:
            assert f" {tool}\n" not in region and f" {tool} " not in region, (
                f"`{tool}` is installed inside a WITH_ANDROID-guarded RUN, so the "
                f":light image would not have it"
            )


def test_gh_install_does_not_precede_the_android_layers():
    """Keep the ~10 GB, hours-long Android/maestro layers cacheable.

    `ansible/playbooks/services.yml` builds with `build: always`, so a cheap apt
    package placed above those layers re-runs them on the next deploy -- and
    sdkmanager has wedged that build twice with no output at all.
    """
    dockerfile = (REPO_ROOT / "services/github-actions-runner/Dockerfile").read_text()

    # Pin where `gh` is INSTALLED, not where the apt repo is declared: an earlier
    # duplicate `apt-get install ... gh` re-runs the layers while the keyring line
    # still sits below. (Caught by mutation-testing this very assertion.)
    gh_installs = [
        offset
        for offset, chunk in _apt_install_chunks(dockerfile)
        if "gh" in _packages_in_chunk(chunk)
    ]
    assert gh_installs, "gh must be installed somewhere"
    assert dockerfile.index("ARG WITH_ANDROID") < min(gh_installs), (
        "the gh install must sit BELOW the Android SDK layers so they stay cached"
    )


def test_runner_version_arg_does_not_precede_the_android_layers():
    """Keep a runner-version bump from re-running the Android/maestro layers.

    Ansible builds this image through `community.docker.docker_image`, which
    drives the daemon's CLASSIC builder. There, changing an ARG's default
    invalidates every layer BELOW the ARG -- not merely the ones that reference
    it. `ACTIONS_RUNNER_VERSION` therefore has to be declared under the Android
    layers, next to the runner download that is its only consumer.

    Declared at the top of the file (where it lived until GitHub deprecated
    2.335.1 and forced a bump) it re-ran ~10 GB of sdkmanager for a ~200 MB
    tarball, extending a total CI outage by hours.
    """
    dockerfile = (REPO_ROOT / "services/github-actions-runner/Dockerfile").read_text()

    declarations = [
        m.start() for m in re.finditer(r"^ARG ACTIONS_RUNNER_VERSION", dockerfile, re.MULTILINE)
    ]
    assert len(declarations) == 1, (
        f"expected exactly one ACTIONS_RUNNER_VERSION declaration, got {len(declarations)}"
    )
    assert dockerfile.index("ARG WITH_ANDROID") < declarations[0], (
        "ARG ACTIONS_RUNNER_VERSION must be declared BELOW the Android SDK "
        "layers, or bumping the runner re-runs them on the classic builder"
    )
