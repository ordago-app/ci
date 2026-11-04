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
from ..provider import (
    ProviderUnavailableError,
    ReviewLimitExceededError,
    ReviewResult,
    SessionRef,
    parse_verdict,
)
from .container import (
    PROMPT_PATH,
    copy_text_file,
    exec_with_deadline,
    remove_existing,
    remove_quietly,
)

CLAUDE_REVIEW_IMAGE = "homelab/claude-review:latest"
CONTAINER_NAME_PREFIX = "claude-review-"

# The image's `node` user.
AGENT_UID = 1000
AGENT_GID = 1000

# Copied into the container, not written into the worktree: the worktree is the code
# under review, this service runs as root, and a PR can commit any path as a symlink.
# The container has no git to ask: a worktree's .git points at a clone outside the
# mount, and Bash is not a tool here.
DIFF_PATH = "/tmp/pr-review.diff"

MAX_TURNS = 60

# `is_error` is the signal. `subtype` said "success" on every failure observed from
# claude 2.1, and no exit code or field is documented for "out of credit", so these
# statuses and messages are matched against what the CLI's errors page lists.
_UNAVAILABLE_STATUSES = {401, 403, 429}
_UNAVAILABLE_RE = re.compile(
    r"usage limit|session limit|spend limit|credit balance|failed to authenticate|invalid api key",
    re.IGNORECASE,
)


_LIMIT_SUBTYPES = {"error_max_budget_usd", "error_max_turns"}


def review_prompt(prompt: str) -> str:
    return (
        f"The PR's full diff, from its merge base to its head, is at {DIFF_PATH}. Read it "
        "first. Your tools are Read, Glob and Grep over /workspace: there is no shell, git "
        "or network tool here.\n\n"
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

        diff = self._worktrees.pr_diff(
            repo_dir=self._projects_root / job.project / "repo",
            base_sha=job.base_sha,
            head_sha=job.head_sha,
        )

        name = f"{CONTAINER_NAME_PREFIX}{job.id}"
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
            copy_text_file(container, DIFF_PATH, diff)
        except BaseException:
            remove_quietly(self._docker, name)
            raise
        return SessionRef(id=str(job.id), container=container.name, worktree=worktree)

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        container = self._docker.containers.get(session.container)
        copy_text_file(container, PROMPT_PATH, review_prompt(prompt))
        # The checkout is the code under review, and its .claude/settings.json could
        # point the CLI at another API endpoint and read this key straight off the wire.
        # --setting-sources "" and --strict-mcp-config ignore everything in it. --bare is
        # NOT enough: it skips hooks but still applies a project's `env`, and it drops
        # Glob and Grep. dontAsk denies whatever would otherwise prompt, which is every
        # read outside the working directory and --add-dir.
        claude = (
            "claude -p --output-format json --no-session-persistence "
            "--tools Read,Glob,Grep --permission-mode dontAsk "
            "--setting-sources '' --strict-mcp-config "
            f"--add-dir {shlex.quote(str(Path(DIFF_PATH).parent))} "
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

    def reap_orphans(self) -> None:
        """Remove review containers a restart left behind: each one holds the API key
        in its environment, sleeping forever. Only safe before the first tick."""
        for container in self._docker.containers.list(
            all=True, filters={"name": CONTAINER_NAME_PREFIX}
        ):
            if container.name.startswith(CONTAINER_NAME_PREFIX):
                remove_quietly(self._docker, container.name)

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
        if payload.get("subtype") in _LIMIT_SUBTYPES:
            raise ReviewLimitExceededError(detail)
        if payload.get("api_error_status") in _UNAVAILABLE_STATUSES or _UNAVAILABLE_RE.search(
            detail
        ):
            raise ProviderUnavailableError(detail)
        raise RuntimeError(f"claude review failed ({payload.get('subtype')}): {detail}")
