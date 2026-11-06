"""A review on one ChatGPT login, with the codex CLI, in the codex-review container.

Codex runs a shell inside its container (`--dangerously-bypass-approvals-and-
sandbox`: the container is the sandbox), so unlike the Claude provider it can
run `git` against the checkout. That is also why its container gets no more
than it needs: the sandbox `sandbox_run_kwargs` describes, the checkout and
the clone's `.git` read-only, and a session copy of the login with no refresh
token in it (codex_auth.py).
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
import threading
from pathlib import Path

from docker import DockerClient
from docker.errors import ImageNotFound

from ..config import CodexAccount
from ..job_store import ReviewJob
from ..provider import (
    ProviderUnavailable,
    ReviewResult,
    SessionRef,
    classify_unavailable,
    parse_verdict,
)
from ._agent_container import (
    container_name,
    diagnostic_lines,
    exec_with_deadline,
    make_agent_writable,
    prompt_on_stdin,
    remove_container,
    remove_container_strict,
    repo_git_volume,
    run_session_container,
    sandbox_run_kwargs,
)
from .codex_auth import CodexAuthOwner

# Headroom past the review's own runtime limit: the CLI refreshes once the token is
# within five minutes of expiry, and a session cannot refresh.
_AUTH_MARGIN_SECONDS = 10 * 60

# Where the session's CODEX_HOME is mounted in the container. Not under the
# image user's home, so the mount does not depend on how the image names it.
CODEX_HOME = "/codex-home"


def render_codex_config(*, model: str) -> str:
    # Empty model = unpinned: emit no `model =` key so the codex CLI uses
    # whatever the ChatGPT subscription currently serves. Pinning an explicit
    # id broke twice when OpenAI rotated the allowlist.
    return f'model = "{model}"\n' if model else ""


def _remove_session_dir(path: Path) -> None:
    """The account's session dir, and the job's once no account is left in it."""
    shutil.rmtree(path, ignore_errors=True)
    with contextlib.suppress(OSError):
        path.parent.rmdir()


def _took_turns(output: str) -> bool:
    """Whether codex produced any item before it failed: then the run was paid."""
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and str(event.get("type", "")).startswith("item."):
            return True
    return False


class CodexReviewProvider:
    def __init__(
        self,
        *,
        account: str,
        settings: CodexAccount,
        docker_client: DockerClient,
        projects_root: Path,
        image: str,
        instance: str,
        auth: CodexAuthOwner | None = None,
    ) -> None:
        self._account = account
        self._settings = settings
        self._docker = docker_client
        self._projects_root = projects_root
        self._image = image
        self._instance = instance
        assert settings.credential.file is not None
        self._auth = auth or CodexAuthOwner(Path(settings.credential.file), account)
        # Container name -> its session directory, deleted with the session.
        self._sessions: dict[str, Path] = {}
        self._sessions_lock = threading.Lock()

    @property
    def account(self) -> str:
        return self._account

    def start_auth_keeper(self, stop: threading.Event) -> None:
        threading.Thread(
            target=self._auth.keep_fresh,
            args=(stop,),
            name=f"codex-auth-keeper-{self._account}",
            daemon=True,
        ).start()

    def start_review_session(
        self, job: ReviewJob, worktree: Path, timeout_seconds: int
    ) -> SessionRef:
        try:
            image = self._docker.images.get(self._image)
        except ImageNotFound as exc:
            raise ProviderUnavailable(
                self._account, "tooling", f"image {self._image} is not built"
            ) from exc
        name = self._container_name(job)
        session_dir = self._session_dir(job)
        # Root writes into it below. Whatever an earlier session left there was
        # writable by its container, so a planted symlink would redirect them.
        shutil.rmtree(session_dir, ignore_errors=True)
        if session_dir.exists():
            raise RuntimeError(f"could not clear the leftover session dir {session_dir}")
        with self._sessions_lock:
            self._sessions[name] = session_dir
        try:
            codex_home = session_dir / "home"
            codex_home.mkdir(parents=True, exist_ok=True)
            (codex_home / "config.toml").write_text(render_codex_config(model=self._settings.model))
            # Mounted read-only over the rw home below: the session reads its
            # login and can never write one. See codex_auth.py.
            session_auth = session_dir / "auth.json"
            self._auth.write_session_copy(session_auth, timeout_seconds + _AUTH_MARGIN_SECONDS)
            # github-review runs as root; the codex container as uid 1000.
            make_agent_writable(codex_home, session_auth)
            run = sandbox_run_kwargs(
                instance=self._instance,
                image_id=image.id,
                worktree=worktree,
                environment={"CODEX_HOME": CODEX_HOME},
            )
            run["volumes"].update(
                {
                    str(codex_home): {"bind": CODEX_HOME, "mode": "rw"},
                    str(session_auth): {"bind": f"{CODEX_HOME}/auth.json", "mode": "ro"},
                    **repo_git_volume(self._projects_root, job.project),
                }
            )
            run_session_container(self._docker, name, **run)
        except BaseException:
            self._forget_session(name)
            raise
        return SessionRef(id=str(job.id), container=name, worktree=worktree)

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        container = self._docker.containers.get(session.container)
        cmd = prompt_on_stdin(
            container,
            prompt,
            ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "-"],
        )
        result = exec_with_deadline(
            container,
            cmd,
            timeout_seconds=timeout_seconds,
            on_timeout=lambda: self.cleanup(session),
            label=self._account,
            stdout=True,
            stderr=True,
        )
        output = result.output.decode("utf-8", errors="replace")
        failure = self._failure_text(output)

        # What the classifier is allowed to see, and nothing more. The
        # transcript carries the model's own prose and the diff it read, and a
        # PR about rate limiting says "usage limit" all over itself — feeding
        # that to the classifier invents outages out of ordinary failures and
        # spends the fallback account on them.
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
        # sticky outage, spends the fallback account re-reviewing the same PR,
        # and stamps the PR with "codex could not run" when codex just did.
        # claude.py gates on `errored` for the same reason.
        if probe and (failure or result.exit_code != 0):
            reason = classify_unavailable(probe)
            if reason is not None:
                raise ProviderUnavailable(
                    self._account, reason, probe.strip()[-800:], read_diff=_took_turns(output)
                )

        # `failure` with no recognised signature is still a failed turn — codex
        # said so itself. Whatever trailing agent_message it left is a partial
        # answer or a refusal, and posting it puts a finding on the PR that
        # nobody wrote (docs/incidents/2026-08-09-phantom-review-verdicts.md). A
        # clean exit code does not redeem it.
        if failure or result.exit_code != 0:
            raise RuntimeError(f"{self._account} review failed: {output.strip()[-1500:]}")

        return parse_verdict(self._extract_review_body(output))

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
            if not isinstance(event, dict):
                continue
            if event.get("type") == "error":
                messages.append(str(event.get("message") or ""))
            elif event.get("type") == "turn.failed":
                messages.append(str((event.get("error") or {}).get("message") or ""))
        return "\n".join(m for m in messages if m)

    def cleanup(self, session: SessionRef) -> None:
        remove_container(self._docker, session.container)
        self._forget_session(session.container)

    def discard(self, job: ReviewJob) -> None:
        remove_container_strict(self._docker, self._container_name(job))
        _remove_session_dir(self._session_dir(job))

    def _session_dir(self, job: ReviewJob) -> Path:
        return self._projects_root / job.project / "review-sessions" / str(job.id) / self._account

    def _forget_session(self, container: str) -> None:
        """Delete the session's directory, and the login copy in it."""
        with self._sessions_lock:
            path = self._sessions.pop(container, None)
        if path is not None:
            _remove_session_dir(path)

    def _container_name(self, job: ReviewJob) -> str:
        return container_name(self._instance, self._account, job.id)

    def _extract_review_body(self, output: str) -> str:
        # codex `exec --json` emits JSONL events; the review is the final
        # agent_message: {"type":"item.completed","item":{"type":
        # "agent_message","text":...}}. Keep the last non-empty one.
        last_text = ""
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
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
                f"[github-review] {self._account} emitted no agent_message; raw output:\n{output}",
                file=sys.stderr,
                flush=True,
            )
            tail = output.strip()[-1500:] or "<empty>"
            raise RuntimeError(f"{self._account} produced no review body; raw tail: {tail}")
        return body
