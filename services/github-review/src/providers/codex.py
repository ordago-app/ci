from __future__ import annotations

import concurrent.futures
import json
import sys
import threading
from pathlib import Path

from docker import DockerClient

from ..config import ToolProfile
from ..job_store import ReviewJob
from ..provider import ProviderUnavailable, ReviewResult, SessionRef, classify_unavailable
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
from .codex_auth import CodexAuthOwner

# Headroom past the review's own runtime limit: the CLI refreshes once the token is
# within five minutes of expiry, and a session cannot refresh.
_AUTH_MARGIN_SECONDS = 10 * 60


def render_codex_config(*, caller: str, mcps: dict[str, list[str]], model: str) -> str:
    # Empty model = unpinned: emit no `model =` key so the codex CLI uses
    # whatever the ChatGPT subscription currently serves. Pinning an explicit
    # id broke twice when OpenAI rotated the allowlist (ADR 0007).
    lines = [f'model = "{model}"', ""] if model else []
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
        self._auth = CodexAuthOwner(codex_state_root)
        # Container name -> its session's login copy, deleted with the session.
        self._session_auth: dict[str, Path] = {}
        self._session_auth_lock = threading.Lock()
        self._router_url = router_url
        self._model = model

    def start_auth_keeper(self, stop: threading.Event) -> None:
        threading.Thread(
            target=self._auth.keep_fresh, args=(stop,), name="codex-auth-keeper", daemon=True
        ).start()

    def start_review_session(
        self, job: ReviewJob, worktree: Path, profile: ToolProfile
    ) -> SessionRef:
        image = self._docker.images.get(f"homelab/codex-code-{job.project}:latest")
        codex_home = self._projects_root / job.project / "review-sessions" / str(job.id)
        codex_home.mkdir(parents=True, exist_ok=True)
        (codex_home / "config.toml").write_text(
            render_codex_config(caller=job.project, mcps=profile.mcps, model=self._model)
        )
        # github-review runs as root; the codex container runs as `agent`
        # (uid 1000). Hand both mounts to that uid so codex can write.
        make_agent_writable(worktree, codex_home)
        # Mounted read-only over the rw home below: the session reads its login
        # and can never write one. See codex_auth.py.
        session_auth = self._session_auth_path(job)
        self._auth.write_session_copy(
            session_auth, profile.max_runtime_minutes * 60 + _AUTH_MARGIN_SECONDS
        )
        make_agent_writable(session_auth)
        name = self._container_name(job)
        with self._session_auth_lock:
            self._session_auth[name] = session_auth
        try:
            self._start_container(job, image.id, worktree, codex_home, session_auth)
        except BaseException:
            self._forget_session_auth(name)
            raise
        return SessionRef(id=str(job.id), container=name, worktree=worktree)

    def _start_container(
        self, job: ReviewJob, image_id: str, worktree: Path, codex_home: Path, session_auth: Path
    ):
        return run_session_container(
            self._docker,
            self._container_name(job),
            image=image_id,
            command=["sleep", "infinity"],
            detach=True,
            network="homelab",
            environment={
                "SESSION_ID": f"review-{job.id}",
                "ROUTER_INTERNAL_URL": self._router_url,
                "AGENT_ROLE": REVIEWER_GH_ROLE,
                # The reviewed repo, so the container's gh/git wrappers can ask
                # the router for a token from the right App installation. A
                # review container is scoped to one repo, unlike this service.
                "GITHUB_REPO": job.repo,
                "CODEX_HOME": "/home/agent/.codex",
            },
            volumes={
                str(worktree): {"bind": "/workspace", "mode": "rw"},
                str(codex_home): {"bind": "/home/agent/.codex", "mode": "rw"},
                str(session_auth): {"bind": "/home/agent/.codex/auth.json", "mode": "ro"},
                **repo_git_volume(self._projects_root, job.project),
            },
        )

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        container = self._docker.containers.get(session.container)
        cmd = prompt_on_stdin(
            container,
            prompt,
            ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "-"],
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(container.exec_run, cmd, stdout=True, stderr=True)
            try:
                result = future.result(timeout=timeout_seconds)
            except concurrent.futures.TimeoutError as exc:
                self.cleanup(session)
                raise RuntimeError("codex review timed out") from exc
        output = result.output.decode("utf-8", errors="replace")
        failure = self._failure_text(output)

        # What the classifier is allowed to see, and nothing more. The
        # transcript carries the model's own prose and the diff it read, and a
        # PR about rate limiting says "usage limit" all over itself — feeding
        # that to the classifier invents outages out of ordinary failures and
        # spends the fallback subscription on them.
        #
        # Structured error events plus the CLI's own non-JSON diagnostics: a
        # transcript only ever travels inside JSON, so everything outside it is
        # diagnostics by construction. Narrowing to "structured events only"
        # would close the leak but drop a real outage that arrives on stderr
        # after the stream has already started, which is the failure class this
        # whole chain exists for.
        probe = "\n".join(x for x in (failure, diagnostic_lines(output, prompt)) if x)

        # Gated on an actual failure signal. `exec_run` folds stderr into this
        # same blob, so a perfectly good run routinely carries non-JSON tracing
        # lines — and classifying those discards a completed review, records a
        # sticky outage, spends the fallback subscription re-reviewing the same
        # PR, and stamps the PR with "codex could not run" when codex just did.
        # Same false outage this file already fixed once, arriving from the
        # success side. claude.py gates on `errored` for the same reason.
        if probe and (failure or result.exit_code != 0):
            reason = classify_unavailable(probe)
            if reason is not None:
                raise ProviderUnavailable("codex", reason, probe.strip()[-800:])

        # `failure` with no recognised signature is still a failed turn — codex
        # said so itself. Whatever trailing agent_message it left is a partial
        # answer or a refusal, and posting it puts a finding on the PR that
        # nobody wrote (incidents/2026-08-09-phantom-review-verdicts.md). A
        # clean exit code does not redeem it.
        if failure or result.exit_code != 0:
            raise RuntimeError(f"codex review failed: {output.strip()[-1500:]}")

        body, event = parse_verdict(self._extract_review_body(output))
        return ReviewResult(body=body, event=event)

    def _failure_text(self, output: str) -> str:
        """Error text from a run that exited 0 anyway, or "".

        The exit code is not the only failure channel: codex can emit an
        `error` / `turn.failed` event and still exit clean, in which case the
        final agent_message — if any — is a refusal rather than a review, and
        posting it would put a phantom finding on the PR. See
        docs/incidents/2026-08-09-phantom-review-verdicts.md.
        """
        messages: list[str] = []
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "error":
                messages.append(str(event.get("message") or ""))
            elif event.get("type") == "turn.failed":
                messages.append(str((event.get("error") or {}).get("message") or ""))
        return "\n".join(m for m in messages if m)

    def cleanup(self, session: SessionRef) -> None:
        remove_container(self._docker, session.container)
        self._forget_session_auth(session.container)

    def discard(self, job: ReviewJob) -> None:
        remove_container_strict(self._docker, self._container_name(job))
        self._session_auth_path(job).unlink(missing_ok=True)

    def _session_auth_path(self, job: ReviewJob) -> Path:
        return self._projects_root / job.project / "review-sessions" / str(job.id) / "auth.json"

    def _forget_session_auth(self, container: str) -> None:
        with self._session_auth_lock:
            path = self._session_auth.pop(container, None)
        if path is not None:
            path.unlink(missing_ok=True)

    @staticmethod
    def _container_name(job: ReviewJob) -> str:
        return f"codex-review-{job.id}"

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
