"""galaxy.yml: what the collection build must never trip over.

The collection is built from the repo root, so everything the repo carries for
its *own* development is a candidate for the tarball unless `build_ignore` says
otherwise. The consumer never wants that -- and one class of file breaks the
build outright rather than merely bloating it.
"""

from fnmatch import fnmatch
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
GALAXY = REPO / "galaxy.yml"
GITMODULES = REPO / ".gitmodules"
SKIP_DIRS = {".git", ".venv", ".ansible", "node_modules", "__pycache__"}


def _build_ignore() -> list[str]:
    return yaml.safe_load(GALAXY.read_text())["build_ignore"]


def _submodule_paths() -> list[Path]:
    paths = []
    for line in GITMODULES.read_text().splitlines():
        key, _, value = line.strip().partition("=")
        if key.strip() == "path":
            paths.append(REPO / value.strip())
    return paths


def _is_ignored(rel: Path, patterns: list[str]) -> bool:
    # ansible-galaxy fnmatches each pattern against the path relative to the
    # collection root; a directory that matches is skipped with its subtree.
    candidates = [rel, *rel.parents[:-1]]
    return any(fnmatch(str(c), p) for c in candidates for p in patterns)


def _symlinks() -> list[Path]:
    found = []
    for path in REPO.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(REPO).parts):
            continue
        if path.is_symlink():
            found.append(path)
    return found


def test_symlinks_into_a_submodule_are_kept_out_of_the_collection_build() -> None:
    """A symlink whose target lives in a submodule dangles in CI.

    `actions/checkout` fetches no submodules, and `ansible-galaxy collection
    build` refuses a dangling symlink instead of skipping it: run 33388725271
    (2026-08-31) died in 10 s on `.agents/skills/ship-a-feature`, the moment the
    shared agent skills were wired in. The agent tooling is not part of the
    collection, so the fix is to `build_ignore` it -- and this test is what
    keeps the next such symlink from re-breaking the build.
    """
    patterns = _build_ignore()
    submodules = _submodule_paths()
    assert submodules, ".gitmodules names no submodule; this test has nothing to guard"

    offenders = []
    for link in _symlinks():
        target = (link.parent / Path(link.readlink())).resolve()
        if not any(target == s or s in target.parents for s in submodules):
            continue
        rel = link.relative_to(REPO)
        if not _is_ignored(rel, patterns):
            offenders.append(str(rel))

    assert not offenders, (
        f"symlinks into a submodule that galaxy.yml build_ignore does not cover: "
        f"{offenders}. They dangle on a checkout without submodules, and "
        "ansible-galaxy collection build fails on a dangling symlink"
    )


def test_agent_tooling_is_not_shipped_to_consumers() -> None:
    """What an agent needs to work on this repo is not what a role needs to run."""
    patterns = _build_ignore()
    for rel in (".agents", ".claude", "CLAUDE.md", "scripts/pr-land.js", ".gitmodules"):
        assert _is_ignored(Path(rel), patterns), f"{rel} would ship inside the collection"
