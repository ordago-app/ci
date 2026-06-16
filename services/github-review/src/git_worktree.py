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
        self._run(self._git(repo_dir, "fetch", "--all", "--prune"))
        present = subprocess.run(
            self._git(repo_dir, "cat-file", "-e", f"{head_sha}^{{commit}}"),
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
        # Drop any stale registration a prior run left behind (cleanup() rmtree's
        # the directory without deregistering), then force the add so a leftover
        # entry never blocks a fresh review.
        self._run(self._git(repo_dir, "worktree", "prune"))
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            self._git(repo_dir, "worktree", "add", "--detach", "--force", str(worktree), head_sha)
        )
        return worktree

    def _git(self, repo_dir: Path, *args: str) -> list[str]:
        # The container runs git as root over a clone owned by the operator
        # (uid 1000). Without this, git aborts with "detected dubious
        # ownership". Scope the exemption to the repo we operate on.
        return ["git", "-c", f"safe.directory={repo_dir}", "-C", str(repo_dir), *args]

    def cleanup(self, worktree: Path) -> None:
        if worktree.exists():
            shutil.rmtree(worktree)

    def _run(self, cmd: list[str]) -> None:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise GitWorktreeError(
                f"command failed: {' '.join(cmd)} stderr={result.stderr.strip()!r}"
            )
