"""A review on one Claude account, in the hardened claude-review container.

Every Claude account runs here, whatever pays for it: the container holds the
account's credential, and the checkout it reads was written by whoever opened
the PR. What keeps the one from reaching the other:

- The CLI gets three tools, Read, Glob and Grep. No shell, no git, no network
  tool, no MCP server: nothing that can send a byte anywhere but the model API.
- `--permission-mode dontAsk` denies every read outside the working directory
  and `--add-dir`, which is /workspace and /tmp — so not /proc, where the
  process's own environment holds the key. A symlink cannot reach it either:
  the service checks worktrees out with `core.symlinks=false`.
- `--setting-sources ''` and `--strict-mcp-config` ignore every settings and
  MCP file, including the checkout's own `.claude/`. Its settings.json could
  otherwise set `env` to point the CLI at another API endpoint and read the key
  off the wire, or add hooks that run with it. `--bare` is NOT enough: it skips
  hooks but still applies a project's `env`, and it drops Glob and Grep.
- The container is the sandbox `sandbox_run_kwargs` describes: non-root, no
  capabilities, bridge network, checkout read-only.

One provider per account, not per model: each holds its own quota, and the
whole point of the next one is that it is still available when the last is
spent. `account` names which one, and it appears in the posted review.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from docker import DockerClient
from docker.errors import ImageNotFound

from ..config import ClaudeAccount
from ..git_worktree import GitWorktreeManager
from ..job_store import ReviewJob
from ..provider import (
    ProviderUnavailable,
    QuotaReading,
    ReviewLimitExceeded,
    ReviewResult,
    SessionRef,
    classify_unavailable,
    parse_verdict,
)
from ._agent_container import (
    WORKSPACE,
    container_name,
    copy_text_file,
    diagnostic_lines,
    exec_with_deadline,
    prompt_on_stdin,
    remove_container,
    remove_container_strict,
    run_session_container,
    sandbox_run_kwargs,
)

# Which variable carries the credential decides who pays: a plan's limits or a
# Console organisation's API credit. A container gets exactly one.
CREDENTIAL_ENV = {"subscription": "CLAUDE_CODE_OAUTH_TOKEN", "api_key": "ANTHROPIC_API_KEY"}

# Copied into the container, not written into the worktree (see copy_text_file).
# The container has no git to ask for it: a worktree's .git points at a clone
# outside the mount, and there is no shell tool here.
DIFF_PATH = "/tmp/pr-review.diff"

MAX_TURNS = 60

# Turn and spend caps. Retrying, or moving to the next account, would spend as
# much again and stop in the same place.
_LIMIT_SUBTYPES = frozenset({"error_max_budget_usd", "error_max_turns"})

# HTTP statuses the CLI reports from the API that say "this account cannot
# review now", whatever the message says around them.
_UNAVAILABLE_STATUSES = {401: "auth", 403: "auth", 429: "exhausted"}

TOOLS = "Read,Glob,Grep"


def review_prompt(prompt: str) -> str:
    """The service's prompt, told what this environment can and cannot do.

    The prompt is shared with providers that have a shell, so it says to run
    git; here the diff is a file instead."""
    return (
        "Environment for this review: your tools are Read, Glob and Grep over "
        f"{WORKSPACE}. There is no shell, git or network tool. The PR's full "
        f"diff, from its merge base to its head, is at {DIFF_PATH}; read it first. "
        "Wherever the instructions below say to run `git diff` or `git grep`, read "
        "that file or use Grep instead, and wherever they mention network access, "
        "work from the PR body and the code.\n\n"
        f"{prompt}"
    )


class ClaudeReviewProvider:
    def __init__(
        self,
        *,
        account: str,
        settings: ClaudeAccount,
        docker_client: DockerClient,
        worktrees: GitWorktreeManager,
        projects_root: Path,
        image: str,
        instance: str,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._account = account
        self._settings = settings
        self._docker = docker_client
        self._worktrees = worktrees
        self._projects_root = projects_root
        self._image = image
        self._instance = instance
        self._environ = environ
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
        self, job: ReviewJob, worktree: Path, timeout_seconds: int
    ) -> SessionRef:
        credential = self._settings.credential.read(self._environ)
        if not credential:
            raise ProviderUnavailable(
                self._account,
                "auth",
                f"no credential in {self._settings.credential.describe()}",
            )
        try:
            image = self._docker.images.get(self._image)
        except ImageNotFound as exc:
            raise ProviderUnavailable(
                self._account, "tooling", f"image {self._image} is not built"
            ) from exc
        diff = self._worktrees.pr_diff(
            repo_dir=self._projects_root / job.project / "repo",
            base_sha=job.base_sha,
            head_sha=job.head_sha,
        )
        if diff is None:
            raise RuntimeError(f"could not compute the diff of {job.head_sha} for {job.repo}")

        name = self._container_name(job)
        container = run_session_container(
            self._docker,
            name,
            **sandbox_run_kwargs(
                instance=self._instance,
                image_id=image.id,
                worktree=worktree,
                environment={CREDENTIAL_ENV[self._settings.auth]: credential},
            ),
        )
        try:
            copy_text_file(container, DIFF_PATH, diff)
        except BaseException:
            remove_container(self._docker, name)
            raise
        return SessionRef(id=str(job.id), container=name, worktree=worktree)

    def argv(self) -> list[str]:
        argv = [
            "claude",
            "-p",
            "--model",
            self._settings.model,
            # JSONL rather than one object, for one reason: only the stream
            # emits `rate_limit_event`, which is how this account's remaining
            # quota reaches the dashboard. It still ends in the same single
            # `result` event `--output-format json` would print.
            "--output-format",
            "stream-json",
            # stream-json refuses to run without it.
            "--verbose",
            "--no-session-persistence",
            "--tools",
            TOOLS,
            "--permission-mode",
            "dontAsk",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--add-dir",
            str(Path(DIFF_PATH).parent),
            "--max-turns",
            str(MAX_TURNS),
        ]
        if self._settings.max_budget_usd is not None:
            argv += ["--max-budget-usd", str(self._settings.max_budget_usd)]
        return argv

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        container = self._docker.containers.get(session.container)
        full_prompt = review_prompt(prompt)
        result = exec_with_deadline(
            container,
            prompt_on_stdin(container, full_prompt, self.argv()),
            timeout_seconds=timeout_seconds,
            on_timeout=lambda: self.cleanup(session),
            label=self._account,
            stdout=True,
            stderr=True,
        )
        output = result.output.decode("utf-8", errors="replace")
        # Before any raise: an exhausted account is exactly when its quota is
        # worth reporting, and every path below this line can throw.
        self._capture_quota(output)
        return self._judge(output, result.exit_code, full_prompt)

    def _judge(self, output: str, exit_code: int, prompt: str) -> ReviewResult:
        event = _result_event(output)
        if event is None:
            # Not one result event: the CLI failed before it got going. The raw
            # text is the only diagnostic there is, so it is classified as such.
            text, subtype, errored = output, None, True
        else:
            text = str(event.get("result") or "")
            subtype = event.get("subtype")
            # `is_error` is the signal; `subtype` said "success" on failures
            # observed from the 2.1 CLI.
            errored = bool(event.get("is_error")) or subtype != "success"

        if subtype in _LIMIT_SUBTYPES:
            raise ReviewLimitExceeded(self._account, _error_detail(event, text))

        # Only error text is ever classified. When `errored` is false, `text` is
        # the review itself — and a review of a PR about rate limiting quotes
        # every signature in the table, so classifying it would manufacture an
        # outage from an ordinary non-zero exit.
        if errored:
            detail = _error_detail(event, text)
            status = event.get("api_error_status") if event is not None else None
            if status in _UNAVAILABLE_STATUSES:
                raise ProviderUnavailable(self._account, _UNAVAILABLE_STATUSES[status], detail)
            probe = "\n".join(x for x in (detail, diagnostic_lines(output, prompt)) if x)
            reason = classify_unavailable(probe)
            if reason is not None:
                raise ProviderUnavailable(self._account, reason, probe.strip()[-800:])
        if errored or exit_code != 0:
            raise RuntimeError(f"{self._account} review failed: {(text or output).strip()[-1500:]}")
        if not text.strip():
            raise RuntimeError(
                f"{self._account} produced no review body; raw tail: {output.strip()[-1500:]}"
            )
        return parse_verdict(text)

    def _capture_quota(self, output: str) -> None:
        """Record the last `rate_limit_event` in a stream-json run, if any.

        Best-effort by construction: a run that died before the event, or a CLI
        that stops emitting it, leaves the previous reading in place and the
        dashboard renders it with its own age. Losing a quota number must never
        cost a review, so nothing here raises.
        """
        for payload in _events_newest_first(output):
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
        return container_name(self._instance, self._account, job.id)


def _events_newest_first(output: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _result_event(output: str) -> dict[str, Any] | None:
    """The run's terminal `result` event, which carries `result`, `is_error`,
    `subtype` and, for an API failure, `api_error_status`."""
    for payload in _events_newest_first(output):
        if payload.get("type") == "result":
            return payload
    return None


def _error_detail(event: dict[str, Any] | None, text: str) -> str:
    errors = event.get("errors") if event is not None else None
    listed = "; ".join(str(e) for e in errors) if isinstance(errors, list) else ""
    subtype = str(event.get("subtype") or "") if event is not None else ""
    return text.strip() or listed or subtype or "no detail"
