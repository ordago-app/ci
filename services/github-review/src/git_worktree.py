from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .github_app import TokenSource

# On every git command the service runs. A PR can commit a symlink to any path —
# `/proc/self/environ` of whatever reads it, a key file on the host — and a
# review container reading the worktree would follow it. Checked out as plain
# files holding the link target, a symlink still reads as what it is in the
# review, and points nowhere.
_HARDENING = ("-c", "core.symlinks=false")


class GitWorktreeError(RuntimeError):
    pass


class GitWorktreeManager:
    def __init__(
        self,
        *,
        projects_root: Path,
        run_as_uid: int | None = None,
        run_as_gid: int | None = None,
        tokens: TokenSource | None = None,
        remote_url: str = "https://github.com/{repo}.git",
    ) -> None:
        self._projects_root = projects_root
        # Credentials for fetching from GitHub. None in tests, whose clones are local.
        self._tokens = tokens
        self._remote_url = remote_url
        # When set (in production the container runs as root), git subprocesses
        # drop to this uid/gid so objects written into the shared clone are
        # owned by the operator (uid 1000), not root. Root-owned objects would
        # otherwise break the operator's `git clone/fetch` on the next deploy.
        # Left None in tests, which run as the unprivileged test user and cannot
        # setuid.
        self._run_as_uid = run_as_uid
        self._run_as_gid = run_as_gid
        # One lock per clone. Concurrent reviews of one repo collide on git's
        # own lock files, and a `worktree prune` drops another job's half-made
        # worktree, so fetching, preparing and removing a worktree each hold
        # it. Reading a commit or a diff does not, and neither does the review.
        self._locks: dict[Path, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    @contextmanager
    def _repo_lock(self, repo_dir: Path) -> Iterator[None]:
        with self._locks_guard:
            lock = self._locks.setdefault(repo_dir.resolve(), threading.Lock())
        with lock:
            yield

    def fetch(self, repo_dir: Path, repo: str) -> None:
        with self._repo_lock(repo_dir):
            self._fetch(repo_dir, repo)

    def _fetch(self, repo_dir: Path, repo: str) -> None:
        """Fetch `repo` into its clone, cloning it first if this instance has none.

        The service owns its clones: an instance on a fresh host starts with an
        empty data directory, and nobody else is there to clone for it. No
        checkout: reviews read worktrees, never the clone's own tree."""
        if not (repo_dir / ".git").exists():
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            self._chown_to_operator(repo_dir.parent)
            self._run(
                [
                    "git",
                    *_HARDENING,
                    "clone",
                    "--no-checkout",
                    self._remote_url.format(repo=repo),
                    str(repo_dir),
                ],
                repo=repo,
            )
        self._run(self._git(repo_dir, "fetch", "--all", "--prune"), repo=repo)

    def prepare(
        self, *, repo_dir: Path, repo: str, project: str, pr_number: int, head_sha: str
    ) -> Path:
        with self._repo_lock(repo_dir):
            return self._prepare(repo_dir, repo, project, pr_number, head_sha)

    def _prepare(
        self, repo_dir: Path, repo: str, project: str, pr_number: int, head_sha: str
    ) -> Path:
        self._fetch(repo_dir, repo)
        present = subprocess.run(
            self._git(repo_dir, "cat-file", "-e", f"{head_sha}^{{commit}}"),
            **self._subprocess_kwargs(),
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
        # The reviews/ dir is created by the (root) Python process; hand it to
        # the operator uid so `git worktree add` running as that uid can write
        # the worktree into it.
        self._chown_to_operator(worktree.parent)
        self._run(
            self._git(repo_dir, "worktree", "add", "--detach", "--force", str(worktree), head_sha)
        )
        return worktree

    def diff_fingerprint(self, *, repo_dir: Path, base_sha: str, head_sha: str) -> str | None:
        """An EXACT identity for what `head_sha` changes relative to its merge-base with
        `base_sha`: sha256 of `git diff --raw --no-abbrev --no-renames`, i.e. every
        changed path with its mode and its before/after blob hashes.

        Blob hashes are over the bytes, so whitespace and binary content count — which
        `git patch-id` does not guarantee (it ignores whitespace, and a text diff drops
        binary content). A clean rebase keeps every blob and only changes commit ids,
        so its fingerprint is unchanged. If the base touched one of the PR's files,
        that file's before-blob differs and so does the fingerprint: no carry, which
        is the safe answer. None when either commit is gone from the clone (a
        force-pushed head is only kept until gc) or the diff is empty."""
        kwargs = self._subprocess_kwargs()
        merge_base = subprocess.run(self._git(repo_dir, "merge-base", base_sha, head_sha), **kwargs)
        if merge_base.returncode != 0:
            return None
        raw = subprocess.run(
            self._git(
                repo_dir,
                "diff",
                "--raw",
                "--no-abbrev",
                "--no-renames",
                merge_base.stdout.strip(),
                head_sha,
            ),
            **kwargs,
        )
        if raw.returncode != 0 or not raw.stdout.strip():
            return None
        return hashlib.sha256(raw.stdout.encode()).hexdigest()

    def pr_diff(self, *, repo_dir: Path, base_sha: str, head_sha: str) -> str | None:
        """What `head_sha` changes relative to its merge-base with `base_sha`: the diff
        GitHub shows on the PR. None when either commit is gone or git fails."""
        kwargs = self._subprocess_kwargs()
        merge_base = subprocess.run(self._git(repo_dir, "merge-base", base_sha, head_sha), **kwargs)
        if merge_base.returncode != 0:
            return None
        return self._diff(repo_dir, merge_base.stdout.strip(), head_sha)

    def diff_since(self, *, repo_dir: Path, previous_head_sha: str, head_sha: str) -> str | None:
        """What was pushed on top of `previous_head_sha` to reach `head_sha`.

        None unless `previous_head_sha` is an ancestor of `head_sha`: after a rebase or
        force-push, a plain diff between the two heads also carries every change the
        base gained, which is not what the author did between rounds."""
        ancestor = subprocess.run(
            self._git(repo_dir, "merge-base", "--is-ancestor", previous_head_sha, head_sha),
            **self._subprocess_kwargs(),
        )
        if ancestor.returncode != 0:
            return None
        return self._diff(repo_dir, previous_head_sha, head_sha)

    def _diff(self, repo_dir: Path, from_sha: str, to_sha: str) -> str | None:
        result = subprocess.run(
            self._git(repo_dir, "diff", "--no-color", "--no-ext-diff", from_sha, to_sha),
            **self._subprocess_kwargs(),
        )
        return result.stdout if result.returncode == 0 else None

    def _git(self, repo_dir: Path, *args: str) -> list[str]:
        # The container runs git as root over a clone owned by the operator
        # (uid 1000). Without this, git aborts with "detected dubious
        # ownership". Scope the exemption to the repo we operate on.
        return [
            "git",
            *_HARDENING,
            "-c",
            f"safe.directory={repo_dir}",
            "-C",
            str(repo_dir),
            *args,
        ]

    def cleanup(self, *, repo_dir: Path, worktree: Path) -> None:
        with self._repo_lock(repo_dir):
            if worktree.exists():
                shutil.rmtree(worktree)

    def _subprocess_kwargs(self, repo: str | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"capture_output": True, "text": True, "check": False}
        env: dict[str, str] = {}
        if self._run_as_uid is not None:
            kwargs["user"] = self._run_as_uid
            if self._run_as_gid is not None:
                kwargs["group"] = self._run_as_gid
            # git as a non-root uid needs a readable/writable HOME; system git
            # config (the credential helper) still applies, so auth is intact.
            env["HOME"] = "/tmp"
        if repo is not None and self._tokens is not None:
            # Read by bin/git-credential-helper, which answers only for this
            # repo: a token in a child's environment never reaches argv or disk.
            env["GIT_REVIEW_REPO"] = repo
            env["GIT_REVIEW_TOKEN"] = self._tokens.token(repo)
        if env:
            kwargs["env"] = {**os.environ, **env}
        return kwargs

    def _chown_to_operator(self, path: Path) -> None:
        if self._run_as_uid is not None:
            os.chown(
                path, self._run_as_uid, self._run_as_gid if self._run_as_gid is not None else -1
            )

    def _run(self, cmd: list[str], repo: str | None = None) -> None:
        result = subprocess.run(cmd, **self._subprocess_kwargs(repo))
        if result.returncode != 0:
            raise GitWorktreeError(
                f"command failed: {' '.join(cmd)} stderr={result.stderr.strip()!r}"
            )
