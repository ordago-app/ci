from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class GitWorktreeError(RuntimeError):
    pass


class GitWorktreeManager:
    def __init__(self, *, projects_root: Path) -> None:
        self._projects_root = projects_root

    def prepare(self, *, repo_dir: Path, project: str, pr_number: int, head_sha: str) -> Path:
        self._run(["git", "-C", str(repo_dir), "fetch", "--all", "--prune"])
        present = subprocess.run(
            ["git", "-C", str(repo_dir), "cat-file", "-e", f"{head_sha}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if present.returncode != 0:
            raise GitWorktreeError(f"head sha {head_sha} not present in {repo_dir}")

        short_sha = head_sha[:12]
        worktree = self._projects_root / project / "reviews" / f"pr-{pr_number}-{short_sha}"
        if worktree.exists():
            shutil.rmtree(worktree)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self._run(["git", "-C", str(repo_dir), "worktree", "add", "--detach", str(worktree), head_sha])
        return worktree

    def cleanup(self, worktree: Path) -> None:
        if worktree.exists():
            shutil.rmtree(worktree)

    def _run(self, cmd: list[str]) -> None:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise GitWorktreeError(
                f"command failed: {' '.join(cmd)} stderr={result.stderr.strip()!r}"
            )
