from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

from docker import DockerClient

from ..config import ToolProfile
from ..job_store import ReviewJob
from ..provider import ProviderUnavailableError, ReviewResult, SessionRef, parse_verdict
from .container import (
    PROMPT_PATH,
    copy_prompt,
    exec_with_deadline,
    remove_existing,
    remove_quietly,
)

# The codex-code agent image runs as this non-root user; the worktree and
# CODEX_HOME it mounts must be owned by it or codex can't write (os error 13).
AGENT_UID = 1000
AGENT_GID = 1000

# The worker is the sole poster. The review container must NOT get a
# write-capable github role, or the model posts its own review via `gh` and
# every PR ends up with duplicate reviews. claude-router does not configure
# this role, so the gh shim / credential helper run unauthenticated (codex
# reads code from the local worktree and the github MCP, never `gh` writes).
CODEX_GH_ROLE = "reviewer-readonly"

# Retrying cannot fix any of these. The long usage cap resets days out, and the
# models-API failure is a CLI the vendor has moved past until its pin is bumped.
_UNAVAILABLE_RE = re.compile(
    r"usage limit|rate limit|out of (extra )?usage|quota|failed to refresh available models",
    re.IGNORECASE,
)


def render_codex_config(*, caller: str, mcps: dict[str, list[str]], model: str) -> str:
    lines = [f'model = "{model}"', ""]
    for mcp_name, caps in mcps.items():
        for cap in caps:
            key = mcp_name if len(caps) == 1 else f"{mcp_name}-{cap}"
            lines.extend(
                [
                    f"[mcp_servers.{key}]",
                    f'url = "http://mcp-gateway:8000/mcp/{mcp_name}/{cap}/mcp"',
                    f'http_headers = {{ "X-MCP-Caller" = "codex-code-server-{caller}" }}',
                    "",
                ]
            )
    return "\n".join(lines)


class CodexReviewProvider:
    def __init__(
        self,
        *,
        docker_client: DockerClient,
        projects_root: Path,
        codex_state_root: Path,
        router_url: str,
        model: str,
    ) -> None:
        self._docker = docker_client
        self._projects_root = projects_root
        self._codex_state_root = codex_state_root
        self._router_url = router_url
        self._model = model

    def start_review_session(
        self, job: ReviewJob, worktree: Path, profile: ToolProfile
    ) -> SessionRef:
        auth = self._codex_state_root / "auth.json"
        if not auth.exists():
            raise ProviderUnavailableError(
                "Codex auth is not onboarded; run `make codex-onboard host=<host>`"
            )

        image = self._docker.images.get(f"homelab/codex-code-{job.project}:latest")
        codex_home = self._projects_root / job.project / "review-sessions" / str(job.id)
        codex_home.mkdir(parents=True, exist_ok=True)
        (codex_home / "config.toml").write_text(
            render_codex_config(caller=job.project, mcps=profile.mcps, model=self._model)
        )
        auth_link = codex_home / "auth.json"
        if auth_link.exists() or auth_link.is_symlink():
            auth_link.unlink()
        auth_link.symlink_to(auth)

        # github-review runs as root; the codex container runs as `agent`
        # (uid 1000). Hand both mounts to that uid so codex can write.
        self._make_agent_writable(worktree, codex_home)

        # `containers.run` is create-then-start. On a loaded host the start can time
        # out after the container exists, and this raises before the worker has a
        # session to clean up -- every retry of the job then died on the name
        # (docs/incidents/2026-10-20-review-container-name-conflict.md). So clear
        # the name first (safe: a job is claimed by exactly one worker, this one)
        # and remove the container if create or start raises.
        name = f"codex-review-{job.id}"
        remove_existing(self._docker, name)
        try:
            container = self._start_container(job, image.id, name, worktree, codex_home)
        except BaseException:
            remove_quietly(self._docker, name)
            raise
        return SessionRef(id=str(job.id), container=container.name, worktree=worktree)

    def _start_container(
        self, job: ReviewJob, image_id: str, name: str, worktree: Path, codex_home: Path
    ):
        return self._docker.containers.run(
            image=image_id,
            name=name,
            command=["sleep", "infinity"],
            detach=True,
            network="homelab",
            environment={
                "SESSION_ID": f"review-{job.id}",
                "ROUTER_INTERNAL_URL": self._router_url,
                "AGENT_ROLE": CODEX_GH_ROLE,
                # The reviewed repo, so the container's gh/git wrappers can ask
                # the router for a token from the right App installation. A
                # review container is scoped to one repo, unlike this service.
                "GITHUB_REPO": job.repo,
                "CODEX_HOME": "/home/agent/.codex",
            },
            volumes={
                str(worktree): {"bind": "/workspace", "mode": "rw"},
                str(codex_home): {"bind": "/home/agent/.codex", "mode": "rw"},
                str(self._codex_state_root): {"bind": str(self._codex_state_root), "mode": "rw"},
            },
        )

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        container = self._docker.containers.get(session.container)
        copy_prompt(container, prompt)
        codex = "codex exec --json --dangerously-bypass-approvals-and-sandbox -"
        result = exec_with_deadline(
            container,
            ["sh", "-c", f"{codex} < {shlex.quote(PROMPT_PATH)}"],
            timeout_seconds=timeout_seconds,
            on_timeout=lambda: self.cleanup(session),
            label="codex",
            stdout=True,
            stderr=True,
        )
        output = result.output.decode("utf-8", errors="replace")
        if result.exit_code != 0:
            reported = _reported_by_codex(output)
            if _UNAVAILABLE_RE.search(reported):
                raise ProviderUnavailableError(reported)
            raise RuntimeError(f"codex review failed: {output.strip()}")
        return parse_verdict(self._extract_review_body(output))

    def cleanup(self, session: SessionRef) -> None:
        remove_quietly(self._docker, session.container)

    def _make_agent_writable(self, *paths: Path) -> None:
        for path in paths:
            if not path.exists() and not path.is_symlink():
                continue
            if path.is_dir() and not path.is_symlink():
                for child in path.rglob("*"):
                    self._chown_for_agent(child)
            self._chown_for_agent(path)

    def _chown_for_agent(self, path: Path) -> None:
        try:
            if path.is_symlink():
                os.lchown(path, AGENT_UID, AGENT_GID)
            else:
                os.chown(path, AGENT_UID, AGENT_GID)
        except PermissionError:
            # When running unprivileged (tests/local) the chown is a no-op; in
            # the container github-review is root, so a failure there is real.
            if os.geteuid() == 0:
                raise

    def _extract_review_body(self, output: str) -> str:
        # codex `exec --json` emits JSONL events; the review is the final
        # agent_message: {"type":"item.completed","item":{"type":
        # "agent_message","text":...}}. (Same schema the telegram codex backend
        # consumes.) Keep the last non-empty agent_message as the review.
        last_text = ""
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                text = str(item.get("text") or "")
                if text:
                    last_text = text
        body = last_text.strip()
        if not body:
            # Dump the full transcript to the container log and surface a tail in
            # the error so "no review body" is diagnosable instead of opaque.
            print(
                f"[github-review] codex emitted no agent_message; raw output:\n{output}",
                file=sys.stderr,
                flush=True,
            )
            tail = output.strip()[-1500:] or "<empty>"
            raise RuntimeError(f"codex produced no review body; raw tail: {tail}")
        return body


def _reported_by_codex(output: str) -> str:
    """The output without its `item.*` events, which carry what the session read and
    ran. A review reads code, and code talks about usage limits."""
    reported: list[str] = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            event = None
        if isinstance(event, dict) and str(event.get("type", "")).startswith("item."):
            continue
        reported.append(line)
    return "\n".join(reported).strip()
