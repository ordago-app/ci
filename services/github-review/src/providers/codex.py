from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import sys
from pathlib import Path

from docker import DockerClient

from ..config import ToolProfile
from ..job_store import ReviewJob
from ..provider import ReviewResult, SessionRef

# The codex-code agent image runs as this non-root user; the worktree and
# CODEX_HOME it mounts must be owned by it or codex can't write (os error 13).
AGENT_UID = 1000
AGENT_GID = 1000


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
            raise RuntimeError("Codex auth is not onboarded; run `make codex-onboard host=<host>`")

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

        container = self._docker.containers.run(
            image=image.id,
            name=f"codex-review-{job.id}",
            command=["sleep", "infinity"],
            detach=True,
            network="homelab",
            environment={
                "SESSION_ID": f"review-{job.id}",
                "ROUTER_INTERNAL_URL": self._router_url,
                "AGENT_ROLE": profile.github_role,
                "CODEX_HOME": "/home/agent/.codex",
            },
            volumes={
                str(worktree): {"bind": "/workspace", "mode": "rw"},
                str(codex_home): {"bind": "/home/agent/.codex", "mode": "rw"},
                str(self._codex_state_root): {"bind": str(self._codex_state_root), "mode": "rw"},
            },
        )
        return SessionRef(id=str(job.id), container=container.name, worktree=worktree)

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        container = self._docker.containers.get(session.container)
        cmd = [
            "codex",
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            prompt,
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(container.exec_run, cmd, stdout=True, stderr=True)
            try:
                result = future.result(timeout=timeout_seconds)
            except concurrent.futures.TimeoutError as exc:
                self.cleanup(session)
                raise RuntimeError("codex review timed out") from exc
        output = result.output.decode("utf-8", errors="replace")
        if result.exit_code != 0:
            raise RuntimeError(f"codex review failed: {output.strip()}")
        body = self._extract_review_body(output)
        return ReviewResult(body=body, event="COMMENT")

    def cleanup(self, session: SessionRef) -> None:
        with contextlib.suppress(Exception):
            self._docker.containers.get(session.container).remove(force=True)

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
