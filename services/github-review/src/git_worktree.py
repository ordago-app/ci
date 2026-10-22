from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
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

    def fetch(self, repo_dir: Path) -> None:
        self._run(self._git(repo_dir, "fetch", "--all", "--prune"))

    def prepare(self, *, repo_dir: Path, project: str, pr_number: int, head_sha: str) -> Path:
        self.fetch(repo_dir)
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

    def _git(self, repo_dir: Path, *args: str) -> list[str]:
        # The container runs git as root over a clone owned by the operator
        # (uid 1000). Without this, git aborts with "detected dubious
        # ownership". Scope the exemption to the repo we operate on.
        return ["git", "-c", f"safe.directory={repo_dir}", "-C", str(repo_dir), *args]

    def cleanup(self, worktree: Path) -> None:
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
