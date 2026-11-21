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


def test_no_symlink_dangles_on_a_plain_checkout() -> None:
    """A dangling symlink kills `ansible-galaxy collection build` outright.

    Run 33388725271 (2026-08-31) died in 10 s on `.agents/skills/ship-a-feature`:
    it pointed into a submodule, and `actions/checkout` fetches no submodules.
    The shared agent skills are a committed copy since then, so every symlink
    must resolve from the tracked files alone -- and no submodule may come back
    to hide one.
    """
    assert not GITMODULES.exists(), (
        ".gitmodules is back: a symlink into a submodule dangles on a checkout "
        "without submodules, which is what CI is"
    )
    dangling = [str(link.relative_to(REPO)) for link in _symlinks() if not link.exists()]
    assert not dangling, f"dangling symlinks, which fail the collection build: {dangling}"


def test_agent_tooling_is_not_shipped_to_consumers() -> None:
    """What an agent needs to work on this repo is not what a role needs to run."""
    patterns = _build_ignore()
    for rel in (".agents", ".claude", "CLAUDE.md", "scripts/pr-land.js"):
        assert _is_ignored(Path(rel), patterns), f"{rel} would ship inside the collection"
