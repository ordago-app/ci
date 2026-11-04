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


def test_run_as_uid_drops_privileges_for_git(tmp_path: Path) -> None:
    # Regression: the container runs as root; git objects it writes into the
    # shared clone must be owned by the operator (uid 1000), not root, or the
    # next deploy's `git fetch` fails with "insufficient permission for adding
    # an object". When run_as_uid is set, every git subprocess must setuid/gid
    # and get a writable HOME.
    mgr = GitWorktreeManager(projects_root=tmp_path, run_as_uid=1000, run_as_gid=1000)
    kwargs = mgr._subprocess_kwargs()
    assert kwargs["user"] == 1000
    assert kwargs["group"] == 1000
    assert kwargs["env"]["HOME"] == "/tmp"


def test_no_uid_means_no_setuid(tmp_path: Path) -> None:
    # Default (tests, which run unprivileged and cannot setuid): no user/group.
    mgr = GitWorktreeManager(projects_root=tmp_path)
    kwargs = mgr._subprocess_kwargs()
    assert "user" not in kwargs
    assert "group" not in kwargs
    assert "env" not in kwargs


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


def test_pr_diff_is_what_the_head_changes_since_the_merge_base(
    repo_dir: Path, tmp_path: Path
) -> None:
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")
    git = ["git", "-C", str(repo_dir)]
    head = subprocess.run([*git, "rev-parse", "feature"], capture_output=True, text=True).stdout
    subprocess.run([*git, "checkout", "-q", "main"], check=True)
    (repo_dir / "moved.txt").write_text("base moved on\n")
    subprocess.run([*git, "add", "moved.txt"], check=True)
    subprocess.run([*git, "commit", "-qm", "base moved"], check=True)
    base = subprocess.run([*git, "rev-parse", "main"], capture_output=True, text=True).stdout

    diff = manager.pr_diff(repo_dir=repo_dir, base_sha=base.strip(), head_sha=head.strip())

    assert "+head" in diff
    assert "moved.txt" not in diff


def test_pr_diff_survives_a_file_that_is_not_utf8(repo_dir: Path, tmp_path: Path) -> None:
    git = ["git", "-C", str(repo_dir)]
    base = sha(repo_dir, "main")
    (repo_dir / "README.md").write_bytes("caf\xe9 latin-1\n".encode("latin-1"))
    subprocess.run([*git, "commit", "-qam", "latin-1"], check=True)
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")

    diff = manager.pr_diff(repo_dir=repo_dir, base_sha=base, head_sha=sha(repo_dir, "feature"))

    assert "latin-1" in diff


def test_pr_diff_raises_when_a_commit_is_gone(repo_dir: Path, tmp_path: Path) -> None:
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")

    with pytest.raises(GitWorktreeError):
        manager.pr_diff(repo_dir=repo_dir, base_sha="0" * 40, head_sha="1" * 40)
