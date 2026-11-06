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


class GitWorktreeError(RuntimeError):
    pass


class GitWorktreeManager:
    def __init__(
        self,
        *,
        projects_root: Path,
        run_as_uid: int | None = None,
        run_as_gid: int | None = None,
    ) -> None:
        self._projects_root = projects_root
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

    def fetch(self, repo_dir: Path) -> None:
        with self._repo_lock(repo_dir):
            self._fetch(repo_dir)

    def _fetch(self, repo_dir: Path) -> None:
        self._run(self._git(repo_dir, "fetch", "--all", "--prune"))

    def prepare(self, *, repo_dir: Path, project: str, pr_number: int, head_sha: str) -> Path:
        with self._repo_lock(repo_dir):
            return self._prepare(repo_dir, project, pr_number, head_sha)

    def _prepare(self, repo_dir: Path, project: str, pr_number: int, head_sha: str) -> Path:
        self._fetch(repo_dir)
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
        return ["git", "-c", f"safe.directory={repo_dir}", "-C", str(repo_dir), *args]

    def cleanup(self, *, repo_dir: Path, worktree: Path) -> None:
        with self._repo_lock(repo_dir):
            if worktree.exists():
                shutil.rmtree(worktree)

    def _subprocess_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"capture_output": True, "text": True, "check": False}
        if self._run_as_uid is not None:
            kwargs["user"] = self._run_as_uid
            if self._run_as_gid is not None:
                kwargs["group"] = self._run_as_gid
            # git as a non-root uid needs a readable/writable HOME; system git
            # config (the credential helper) still applies, so auth is intact.
            kwargs["env"] = {**os.environ, "HOME": "/tmp"}
        return kwargs

    def _chown_to_operator(self, path: Path) -> None:
        if self._run_as_uid is not None:
            os.chown(
                path, self._run_as_uid, self._run_as_gid if self._run_as_gid is not None else -1
            )

    def _run(self, cmd: list[str]) -> None:
        result = subprocess.run(cmd, **self._subprocess_kwargs())
        if result.returncode != 0:
            raise GitWorktreeError(
                f"command failed: {' '.join(cmd)} stderr={result.stderr.strip()!r}"
            )
