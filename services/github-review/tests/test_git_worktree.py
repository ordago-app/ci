import shutil
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


def test_prepare_recovers_from_stale_registered_worktree(tmp_path: Path, repo_dir: Path) -> None:
    # Regression: a prior failed/cleaned run leaves the worktree registered in
    # .git/worktrees even though its directory was rmtree'd, so the next
    # `worktree add` aborts with "missing but already registered". prepare()
    # must be self-healing across runs.
    head = sha(repo_dir, "feature")
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")
    first = manager.prepare(repo_dir=repo_dir, project="homelab", pr_number=5, head_sha=head)
    # Simulate cleanup(): directory gone, git registration left behind.
    shutil.rmtree(first)
    second = manager.prepare(repo_dir=repo_dir, project="homelab", pr_number=5, head_sha=head)
    assert (second / "README.md").read_text() == "head\n"
    assert sha(second, "HEAD") == head


def test_git_commands_carry_safe_directory_guard(
    tmp_path: Path, repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: in production the clone is owned by uid 1000 but the container
    # runs git as root, so plain `git -C <repo>` fails with "detected dubious
    # ownership" and every review job dies before Codex runs. Every git
    # invocation must opt the repo into safe.directory to be ownership-agnostic.
    head = sha(repo_dir, "feature")
    calls: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(cmd: list[str], *args: object, **kwargs: object) -> object:
        calls.append(cmd)
        return real_run(cmd, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", recording_run)
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")
    manager.prepare(repo_dir=repo_dir, project="homelab", pr_number=5, head_sha=head)

    git_calls = [cmd for cmd in calls if cmd and cmd[0] == "git"]
    assert git_calls, "expected git to be invoked"
    for cmd in git_calls:
        assert f"safe.directory={repo_dir}" in cmd, f"git call missing safe.directory guard: {cmd}"
