from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any, NoReturn

from docker import DockerClient
from docker.errors import ImageNotFound

from ..config import ToolProfile
from ..git_worktree import GitWorktreeManager
from ..job_store import ReviewJob
from ..provider import ProviderUnavailableError, ReviewResult, SessionRef, parse_verdict
from .container import (
    PROMPT_PATH,
    copy_prompt,
    exec_with_deadline,
    remove_existing,
    remove_quietly,
)

CLAUDE_REVIEW_IMAGE = "homelab/claude-review:latest"

# The image's `node` user, which also owns the worktree the review mounts.
AGENT_UID = 1000
AGENT_GID = 1000

# Written into the worktree before it is mounted. The container has no git to ask:
# a worktree's .git points at a clone outside the mount, and Bash is not a tool here.
DIFF_FILENAME = ".pr-review.diff"

MAX_TURNS = 60

# `is_error` is the signal. `subtype` said "success" on every failure observed from
# claude 2.1, and no exit code or field is documented for "out of credit", so these
# statuses and messages are matched against what the CLI's errors page lists.
_UNAVAILABLE_STATUSES = {401, 403, 429}
_UNAVAILABLE_RE = re.compile(
    r"usage limit|session limit|spend limit|credit balance|failed to authenticate|invalid api key",
    re.IGNORECASE,
)


def review_prompt(prompt: str) -> str:
    return (
        f"The PR's full diff, from its merge base to its head, is at "
        f"/workspace/{DIFF_FILENAME}. Read it first. Your tools are Read, Glob and Grep "
        "over /workspace: there is no shell, git or network tool here.\n\n"
        f"{prompt}"
    )


class ClaudeReviewProvider:
    def __init__(
        self,
        *,
        docker_client: DockerClient,
        worktrees: GitWorktreeManager,
        projects_root: Path,
        api_key_file: Path,
        model: str,
        max_budget_usd: float,
    ) -> None:
        self._docker = docker_client
        self._worktrees = worktrees
        self._projects_root = projects_root
        self._api_key_file = api_key_file
        self._model = model
        self._max_budget_usd = max_budget_usd

    def start_review_session(
        self, job: ReviewJob, worktree: Path, profile: ToolProfile
    ) -> SessionRef:
        api_key = self._read_api_key()
        try:
            image = self._docker.images.get(CLAUDE_REVIEW_IMAGE)
        except ImageNotFound as exc:
            raise ProviderUnavailableError(f"image {CLAUDE_REVIEW_IMAGE} is not built") from exc

        (worktree / DIFF_FILENAME).write_text(
            self._worktrees.pr_diff(
                repo_dir=self._projects_root / job.project / "repo",
                base_sha=job.base_sha,
                head_sha=job.head_sha,
            )
        )

        name = f"claude-review-{job.id}"
        remove_existing(self._docker, name)
        try:
            container = self._docker.containers.run(
                image=image.id,
                name=name,
                command=["sleep", "infinity"],
                detach=True,
                # It only needs Anthropic's API: no MCP gateway, no github token.
                # The checkout is the code under review, so it stays off the
                # operator's service network and cannot write to its own mount.
                network="bridge",
                user=f"{AGENT_UID}:{AGENT_GID}",
                working_dir="/workspace",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                environment={"ANTHROPIC_API_KEY": api_key},
                volumes={str(worktree): {"bind": "/workspace", "mode": "ro"}},
            )
        except BaseException:
            remove_quietly(self._docker, name)
            raise
        return SessionRef(id=str(job.id), container=container.name, worktree=worktree)

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        container = self._docker.containers.get(session.container)
        copy_prompt(container, review_prompt(prompt))
        # --bare skips the checkout's own hooks and MCP servers, and it is why this
        # provider takes an API key: --bare does not read a subscription token.
        # dontAsk denies whatever would otherwise prompt, which is every read
        # outside the working directory.
        claude = (
            "claude --bare -p --output-format json --no-session-persistence "
            "--tools Read,Glob,Grep --permission-mode dontAsk "
            f"--model {shlex.quote(self._model)} --max-turns {MAX_TURNS} "
            f"--max-budget-usd {self._max_budget_usd}"
        )
        result = exec_with_deadline(
            container,
            ["sh", "-c", f"{claude} < {shlex.quote(PROMPT_PATH)}"],
            timeout_seconds=timeout_seconds,
            on_timeout=lambda: self.cleanup(session),
            label="claude",
            stdout=True,
            stderr=True,
            demux=True,
        )
        stdout, stderr = result.output
        return self._parse_output(
            (stdout or b"").decode("utf-8", errors="replace"),
            (stderr or b"").decode("utf-8", errors="replace"),
            result.exit_code,
        )

    def cleanup(self, session: SessionRef) -> None:
        remove_quietly(self._docker, session.container)

    def _read_api_key(self) -> str:
        try:
            api_key = self._api_key_file.read_text().strip()
        except OSError as exc:
            raise ProviderUnavailableError(
                f"no Anthropic API key at {self._api_key_file}: {exc}"
            ) from exc
        if not api_key:
            raise ProviderUnavailableError(
                f"the Anthropic API key at {self._api_key_file} is empty"
            )
        return api_key

    def _parse_output(self, stdout: str, stderr: str, exit_code: int) -> ReviewResult:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"claude review failed with exit {exit_code}: {stderr.strip() or stdout.strip()}"
            ) from exc

        text = str(payload.get("result") or "").strip()
        failed = payload.get("is_error") or exit_code != 0 or payload.get("subtype") != "success"
        if failed:
            self._raise_failure(payload, text)
        if not text:
            raise RuntimeError(f"claude produced no review body: {stdout.strip()}")
        return parse_verdict(text)

    def _raise_failure(self, payload: dict[str, Any], text: str) -> NoReturn:
        detail = text or "; ".join(str(e) for e in payload.get("errors") or []) or str(payload)
        if payload.get("api_error_status") in _UNAVAILABLE_STATUSES or _UNAVAILABLE_RE.search(
            detail
        ):
            raise ProviderUnavailableError(detail)
        raise RuntimeError(f"claude review failed ({payload.get('subtype')}): {detail}")
