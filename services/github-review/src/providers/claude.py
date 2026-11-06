from __future__ import annotations

import concurrent.futures
import json
import time
from pathlib import Path
from typing import Literal

from docker import DockerClient

from ..config import ToolProfile
from ..job_store import ReviewJob
from ..provider import (
    ProviderUnavailable,
    QuotaReading,
    ReviewResult,
    SessionRef,
    classify_unavailable,
)
from ._agent_container import (
    REVIEWER_GH_ROLE,
    diagnostic_lines,
    make_agent_writable,
    parse_verdict,
    prompt_on_stdin,
    remove_container,
    remove_container_strict,
    repo_git_volume,
    run_session_container,
)

GATEWAY_BASE_URL = "http://mcp-gateway:8000"

# The variable decides who pays: a subscription's limits, or a Console
# organisation's API credits. A container gets exactly one of them.
ClaudeCredentialEnv = Literal["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"]


def render_mcp_json(*, caller: str, mcps: dict[str, list[str]]) -> str:
    """Body for the container's `.claude/.mcp.json`.

    Mirrors claude-router's `mcp_renderer.render_mcp_json`; it cannot be
    imported, because that lives in a different service and image.

    The transport field is named `type`, not `transport`. The wrong key yields
    a syntactically valid config that claude treats as having no servers at
    all — a reviewer that quietly cannot see GitHub.
    """
    servers: dict[str, dict[str, object]] = {}
    for mcp_name, caps in mcps.items():
        if not caps:
            continue
        single_cap = len(caps) == 1
        for cap in caps:
            key = mcp_name if single_cap else f"{mcp_name}-{cap}"
            servers[key] = {
                "type": "http",
                "url": f"{GATEWAY_BASE_URL}/mcp/{mcp_name}/{cap}/mcp",
                "headers": {"X-MCP-Caller": caller},
            }
    return json.dumps({"mcpServers": servers}, indent=2)


class ClaudeReviewProvider:
    """A review on one Claude account: a subscription, or a Console API key.

    One instance per account, not per model: each holds its own quota, and the
    whole point of the next one is that it is still available when the last is
    spent. `account` names which one, and it appears in the posted review — a
    review nobody can attribute is a review nobody can trust.
    """

    def __init__(
        self,
        *,
        account: str,
        credential_env: ClaudeCredentialEnv,
        credential: str,
        docker_client: DockerClient,
        projects_root: Path,
        router_url: str,
        model: str,
    ) -> None:
        self._account = account
        self._credential_env = credential_env
        self._credential = credential
        self._docker = docker_client
        self._projects_root = projects_root
        self._router_url = router_url
        self._model = model
        # Last quota reading seen from this account, or None until it has run
        # once. In memory to match `_outages` in the worker: it describes now,
        # and after a restart the number should be re-earned by a real review
        # rather than replayed from disk at whatever staleness.
        self._last_quota: QuotaReading | None = None

    @property
    def account(self) -> str:
        return self._account

    @property
    def last_quota(self) -> QuotaReading | None:
        """The most recent `rate_limit_event` this account reported."""
        return self._last_quota

    def start_review_session(
        self, job: ReviewJob, worktree: Path, profile: ToolProfile
    ) -> SessionRef:
        if not self._credential:
            raise ProviderUnavailable(
                self._account,
                "auth",
                f"no credential for {self._credential_env}; "
                "see secrets/secrets.example.yml and set it in secrets.prod.yml",
            )

        image = self._docker.images.get(f"homelab/claude-code-{job.project}:latest")

        # Per-account config dir, never shared. Two accounts under one
        # CLAUDE_CONFIG_DIR make the CLI reuse the first account's cached
        # credentials, so the fallback would spend the very window it fled.
        config_dir = (
            self._projects_root / job.project / "review-sessions" / str(job.id) / self._account
        )
        (config_dir / "projects").mkdir(parents=True, exist_ok=True)

        # Into the config dir, NEVER the worktree. claude does not
        # auto-discover this path, so run_review passes it explicitly with
        # --mcp-config — which is exactly what lets it live somewhere safe.
        #
        # The worktree is a checkout of the PR head, i.e. ground the branch
        # under review controls, and this service runs as root OUTSIDE the
        # review container. A branch that commits `.claude` as a symlink gets
        # `mkdir(exist_ok=True)` to succeed (is_dir follows it) and this write
        # to land wherever the link points; committing it as a regular file
        # raises FileExistsError, which is not ProviderUnavailable and so fails
        # the job without failing over. Both verified. claude-router writes into
        # its worktree, but that worktree is the agent's own — not an arbitrary
        # contributor branch.
        (config_dir / ".mcp.json").write_text(
            render_mcp_json(caller=f"claude-code-server-{job.project}", mcps=profile.mcps)
        )

        # github-review runs as root; the agent container runs as uid 1000.
        make_agent_writable(worktree, config_dir)

        container = run_session_container(
            self._docker,
            self._container_name(job),
            image=image.id,
            command=["sleep", "infinity"],
            detach=True,
            network="homelab",
            environment={
                "SESSION_ID": f"review-{job.id}",
                "ROUTER_INTERNAL_URL": self._router_url,
                self._credential_env: self._credential,
                "CLAUDE_CONFIG_DIR": "/home/agent/.claude",
                # The overlay's gh / git-credential wrappers read CLAUDE_ROLE
                # (not AGENT_ROLE, which is codex's name for the same thing).
                "CLAUDE_ROLE": REVIEWER_GH_ROLE,
                "GITHUB_REPO": job.repo,
                # The container is already the trust boundary; the extra
                # bubblewrap Bash sandbox adds overhead, not safety. Same call
                # the router makes for every agent session.
                "CLAUDE_CODE_BUBBLEWRAP": "0",
            },
            volumes={
                str(worktree): {"bind": "/workspace", "mode": "rw"},
                str(config_dir): {"bind": "/home/agent/.claude", "mode": "rw"},
                **repo_git_volume(self._projects_root, job.project),
            },
        )
        return SessionRef(id=str(job.id), container=container.name, worktree=worktree)

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        container = self._docker.containers.get(session.container)
        argv = [
            "claude",
            "-p",
            "--model",
            self._model,
            # JSONL rather than one object, for one reason: only the stream
            # emits `rate_limit_event`, which is how this account's remaining
            # quota reaches the dashboard. `--output-format json` carries token
            # counts and cost but no rate-limit field at all (verified against
            # the CLI, 2026-09-04). Nothing is reassembled to pay for it — the
            # stream still ends in the same single `result` event this already
            # read, so `_extract_result` is unchanged.
            "--output-format",
            "stream-json",
            # stream-json refuses to run without it.
            "--verbose",
            # Explicit, because claude only auto-discovers ./.mcp.json — and
            # --strict-mcp-config so ONLY this file is honoured. Without the
            # second flag the reviewer, running in the PR's own worktree with
            # --dangerously-skip-permissions, would honour an .mcp.json the
            # branch under review shipped.
            "--mcp-config",
            "/home/agent/.claude/.mcp.json",
            "--strict-mcp-config",
            # User settings only: the per-session config dir. The worktree's
            # `.claude/settings.json` belongs to the branch under review, and
            # its hooks would run inside a container holding the credential,
            # and its SessionStart output would ride along on every turn.
            "--setting-sources",
            "user",
            "--dangerously-skip-permissions",
        ]
        cmd = prompt_on_stdin(container, prompt, argv)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                container.exec_run, cmd, stdout=True, stderr=True, workdir="/workspace"
            )
            try:
                result = future.result(timeout=timeout_seconds)
            except concurrent.futures.TimeoutError as exc:
                self.cleanup(session)
                raise RuntimeError(f"{self._account} review timed out") from exc

        output = result.output.decode("utf-8", errors="replace")
        # Before any raise: an exhausted account is exactly when its quota is
        # worth reporting, and every path below this line can throw.
        self._capture_quota(output)
        text, errored = self._extract_result(output)

        # Only error text is ever classified. When `errored` is false, `text` is
        # the review itself — and a review of a PR about rate limiting quotes
        # every signature in the table, so classifying it would manufacture an
        # outage from an ordinary non-zero exit.
        if errored:
            probe = "\n".join(x for x in (text, diagnostic_lines(output, prompt)) if x)
            reason = classify_unavailable(probe)
            if reason is not None:
                raise ProviderUnavailable(self._account, reason, probe.strip()[-800:])
        if errored or result.exit_code != 0:
            raise RuntimeError(f"{self._account} review failed: {(text or output).strip()[-1500:]}")

        if not text.strip():
            raise RuntimeError(
                f"{self._account} produced no review body; raw tail: {output.strip()[-1500:]}"
            )
        body, event = parse_verdict(text)
        return ReviewResult(body=body, event=event)

    def _capture_quota(self, output: str) -> None:
        """Record the last `rate_limit_event` in a stream-json run, if any.

        Best-effort by construction: a run that died before the event, or a CLI
        that stops emitting it, leaves the previous reading in place and the
        dashboard renders it with its own age. Losing a quota number must never
        cost a review, so nothing here raises.
        """
        for line in reversed(output.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") != "rate_limit_event":
                continue
            info = payload.get("rate_limit_info")
            if not isinstance(info, dict):
                return
            # `utilization` is absent until a threshold is surpassed. Absent is
            # "the CLI reported no number", which is NOT zero — coercing it to
            # 0.0 would render a silent account as a freshly-reset one.
            raw_utilization = info.get("utilization")
            self._last_quota = QuotaReading(
                account=self._account,
                status=str(info.get("status") or "unknown"),
                limit_type=str(info.get("rateLimitType") or ""),
                utilization=(
                    float(raw_utilization) if isinstance(raw_utilization, int | float) else None
                ),
                resets_at=(
                    float(info["resetsAt"])
                    if isinstance(info.get("resetsAt"), int | float)
                    else None
                ),
                observed_at=time.time(),
            )
            return

    def cleanup(self, session: SessionRef) -> None:
        remove_container(self._docker, session.container)

    def discard(self, job: ReviewJob) -> None:
        remove_container_strict(self._docker, self._container_name(job))

    def _container_name(self, job: ReviewJob) -> str:
        return f"claude-review-{job.id}-{self._account}"

    def _extract_result(self, output: str) -> tuple[str, bool]:
        """Return (text, errored) from the run's terminal `result` event.

        Claude Code ends both `--output-format json` and the stream-json JSONL
        with the same object, carrying `result` and `is_error`; scanning in
        reverse for it reads either.
        A body that is not JSON at all is the CLI failing before it ever got
        going — hand the raw text back so the caller can classify it, rather
        than swallowing the only diagnostic there is.
        """
        for line in reversed(output.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") != "result":
                continue
            return str(payload.get("result") or ""), bool(payload.get("is_error"))
        return output, True
