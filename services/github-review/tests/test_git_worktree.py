import subprocess
from pathlib import Path

import pytest

from src.git_worktree import GitWorktreeError, GitWorktreeManager


def sha(repo: Path, ref: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", ref], text=True).strip()


def test_creates_review_worktree_at_expected_sha(tmp_path: Path, repo_dir: Path) -> None:
    head = sha(repo_dir, "feature")
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")
    worktree = manager.prepare(repo_dir=repo_dir, project="homelab", pr_number=5, head_sha=head)
    assert (worktree / "README.md").read_text() == "head\n"
    assert sha(worktree, "HEAD") == head


def test_rejects_sha_mismatch(tmp_path: Path, repo_dir: Path) -> None:
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")
    with pytest.raises(GitWorktreeError, match="not present"):
        manager.prepare(repo_dir=repo_dir, project="homelab", pr_number=5, head_sha="0" * 40)
