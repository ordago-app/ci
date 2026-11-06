import shutil
import subprocess
from pathlib import Path

import pytest
from src.git_worktree import GitWorktreeError, GitWorktreeManager

REPO = "owner/name"


def sha(repo: Path, ref: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", ref], text=True).strip()


def test_creates_review_worktree_at_expected_sha(tmp_path: Path, repo_dir: Path) -> None:
    head = sha(repo_dir, "feature")
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")
    worktree = manager.prepare(
        repo_dir=repo_dir, repo=REPO, project="homelab", pr_number=5, head_sha=head
    )
    assert (worktree / "README.md").read_text() == "head\n"
    assert sha(worktree, "HEAD") == head


def test_rejects_sha_mismatch(tmp_path: Path, repo_dir: Path) -> None:
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")
    with pytest.raises(GitWorktreeError, match="not present"):
        manager.prepare(
            repo_dir=repo_dir, repo=REPO, project="homelab", pr_number=5, head_sha="0" * 40
        )


def test_prepare_recovers_from_stale_registered_worktree(tmp_path: Path, repo_dir: Path) -> None:
    # Regression: a prior failed/cleaned run leaves the worktree registered in
    # .git/worktrees even though its directory was rmtree'd, so the next
    # `worktree add` aborts with "missing but already registered". prepare()
    # must be self-healing across runs.
    head = sha(repo_dir, "feature")
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")
    first = manager.prepare(
        repo_dir=repo_dir, repo=REPO, project="homelab", pr_number=5, head_sha=head
    )
    # Simulate cleanup(): directory gone, git registration left behind.
    shutil.rmtree(first)
    second = manager.prepare(
        repo_dir=repo_dir, repo=REPO, project="homelab", pr_number=5, head_sha=head
    )
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
    manager.prepare(repo_dir=repo_dir, repo=REPO, project="homelab", pr_number=5, head_sha=head)

    git_calls = [cmd for cmd in calls if cmd and cmd[0] == "git"]
    assert git_calls, "expected git to be invoked"
    for cmd in git_calls:
        assert f"safe.directory={repo_dir}" in cmd, f"git call missing safe.directory guard: {cmd}"


def test_concurrent_prepares_on_one_repo_never_overlap_git_writes(
    tmp_path: Path, repo_dir: Path
) -> None:
    # Concurrent reviews of one repo: fetch / worktree prune / worktree add
    # collide on git's lock files, and a prune drops another job's half-made
    # worktree. Every git write on a project must run alone.
    import threading
    import time

    head = sha(repo_dir, "feature")
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")
    in_flight = 0
    overlaps = 0
    guard = threading.Lock()
    real_run = manager._run

    def tracked_run(cmd: list[str], repo: str | None = None) -> None:
        nonlocal in_flight, overlaps
        with guard:
            in_flight += 1
            overlaps += in_flight > 1
        time.sleep(0.01)
        try:
            real_run(cmd, repo)
        finally:
            with guard:
                in_flight -= 1

    manager._run = tracked_run  # type: ignore[method-assign]
    worktrees: list[Path] = []

    def prepare(pr: int) -> None:
        worktrees.append(
            manager.prepare(
                repo_dir=repo_dir, repo=REPO, project="homelab", pr_number=pr, head_sha=head
            )
        )
        manager.fetch(repo_dir, REPO)

    threads = [threading.Thread(target=prepare, args=(pr,)) for pr in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlaps == 0
    assert len(worktrees) == 6
    assert all(sha(w, "HEAD") == head for w in worktrees)


def test_cleanup_waits_for_other_git_work_on_the_same_clone(tmp_path: Path, repo_dir: Path) -> None:
    import threading

    head = sha(repo_dir, "feature")
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")
    worktree = manager.prepare(
        repo_dir=repo_dir, repo=REPO, project="homelab", pr_number=1, head_sha=head
    )
    done = threading.Event()
    with manager._repo_lock(repo_dir):
        threading.Thread(
            target=lambda: (manager.cleanup(repo_dir=repo_dir, worktree=worktree), done.set())
        ).start()
        assert not done.wait(0.1), "cleanup must not run while the clone is locked"
    assert done.wait(5)
    assert not worktree.exists()


def test_pr_diff_is_what_the_head_changes_since_the_merge_base(
    repo_dir: Path, tmp_path: Path
) -> None:
    subprocess.run(["git", "-C", str(repo_dir), "checkout", "-q", "main"], check=True)
    (repo_dir / "other.txt").write_text("base moved\n")
    subprocess.run(["git", "-C", str(repo_dir), "add", "other.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "base moved"], check=True)
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")

    diff = manager.pr_diff(
        repo_dir=repo_dir, base_sha=sha(repo_dir, "main"), head_sha=sha(repo_dir, "feature")
    )

    assert diff is not None
    assert "-base\n+head" in diff
    assert "other.txt" not in diff


def test_diff_since_is_only_what_was_pushed_on_top(repo_dir: Path, tmp_path: Path) -> None:
    previous = sha(repo_dir, "feature")
    (repo_dir / "README.md").write_text("fixed\n")
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-am", "fix"], check=True)
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")

    diff = manager.diff_since(
        repo_dir=repo_dir, previous_head_sha=previous, head_sha=sha(repo_dir, "feature")
    )

    assert diff is not None
    assert "-head\n+fixed" in diff


def test_diff_since_a_head_that_was_rewritten_away_is_none(repo_dir: Path, tmp_path: Path) -> None:
    previous = sha(repo_dir, "feature")
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-q", "--amend", "-m", "amended"], check=True
    )
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")

    assert (
        manager.diff_since(
            repo_dir=repo_dir, previous_head_sha=previous, head_sha=sha(repo_dir, "feature")
        )
        is None
    )


def test_a_worktree_answers_git_through_the_clone_it_names(repo_dir: Path, tmp_path: Path) -> None:
    """The review container mounts the clone's `.git` at the path the worktree's
    `.git` file names (providers._agent_container.repo_git_volume): that path must
    be the clone's own, or git in the container says "not a git repository"."""
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")
    worktree = manager.prepare(
        repo_dir=repo_dir,
        repo=REPO,
        project="homelab",
        pr_number=5,
        head_sha=sha(repo_dir, "feature"),
    )
    gitdir = Path((worktree / ".git").read_text().removeprefix("gitdir: ").strip())
    assert gitdir.parent.parent == repo_dir / ".git"


def test_a_committed_symlink_is_checked_out_as_a_plain_file(tmp_path: Path, repo_dir: Path) -> None:
    """A PR can commit `leak -> /proc/self/environ`. Followed inside a review
    container, Read would hand the model the process's own environment, the
    credential with it. Checked out as a plain file, it only names its target."""
    (repo_dir / "leak").symlink_to("/proc/self/environ")
    subprocess.run(["git", "-C", str(repo_dir), "add", "leak"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-qm", "leak"], check=True)
    manager = GitWorktreeManager(projects_root=tmp_path / "projects")

    worktree = manager.prepare(
        repo_dir=repo_dir, repo=REPO, project="homelab", pr_number=5, head_sha=sha(repo_dir, "HEAD")
    )

    assert not (worktree / "leak").is_symlink()
    assert (worktree / "leak").read_text() == "/proc/self/environ"


def test_an_instance_clones_a_repo_it_has_never_seen(tmp_path: Path, repo_dir: Path) -> None:
    """A fresh host has an empty data directory and nobody to clone for it."""
    manager = GitWorktreeManager(
        projects_root=tmp_path / "projects", remote_url=f"{repo_dir.parent}/{{repo}}"
    )
    clone = tmp_path / "projects" / "homelab" / "repo"

    worktree = manager.prepare(
        repo_dir=clone,
        repo=repo_dir.name,
        project="homelab",
        pr_number=5,
        head_sha=sha(repo_dir, "feature"),
    )

    assert (clone / ".git").is_dir()
    assert (worktree / "README.md").read_text() == "head\n"


class _Tokens:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def token(self, repo: str, *, refresh: bool = False) -> str:
        self.asked.append(repo)
        return f"tok-{repo}"


def test_git_gets_the_token_for_the_repo_it_fetches_in_its_environment(tmp_path: Path) -> None:
    tokens = _Tokens()
    mgr = GitWorktreeManager(projects_root=tmp_path, tokens=tokens)

    env = mgr._subprocess_kwargs("o/r")["env"]

    assert (env["GIT_REVIEW_REPO"], env["GIT_REVIEW_TOKEN"]) == ("o/r", "tok-o/r")
    assert "GIT_REVIEW_TOKEN" not in mgr._subprocess_kwargs().get("env", {})


def _helper(stdin: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    helper = Path(__file__).resolve().parents[1] / "bin" / "git-credential-helper"
    return subprocess.run(
        [str(helper), "get"], input=stdin, env=env, capture_output=True, text=True, check=False
    )


def test_the_credential_helper_answers_only_for_its_own_repo() -> None:
    env = {"PATH": "/usr/bin:/bin", "GIT_REVIEW_REPO": "o/r", "GIT_REVIEW_TOKEN": "tok"}

    mine = _helper("protocol=https\nhost=github.com\npath=o/r.git\n\n", env)
    other = _helper("protocol=https\nhost=github.com\npath=o/other.git\n\n", env)
    elsewhere = _helper("protocol=https\nhost=evil.example\npath=o/r.git\n\n", env)
    no_path = _helper("protocol=https\nhost=github.com\n\n", env)

    assert mine.stdout == "username=x-access-token\npassword=tok\n"
    assert other.stdout == elsewhere.stdout == ""
    assert no_path.returncode == 78
