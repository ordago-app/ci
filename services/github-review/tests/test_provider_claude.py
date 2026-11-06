"""The Claude side of the failover chain.

One provider instance per subscription: the point of the second Anthropic
account is that it has its own usage window, so the two must never share
credentials, container names, or config state.
"""

import contextlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from src.job_store import JobStatus, ReviewJob
from src.provider import ProviderUnavailable, SessionRef
from src.providers import _agent_container as agent_container
from src.providers._agent_container import PROMPT_PATH
from src.providers.claude import ClaudeCredentialEnv, ClaudeReviewProvider, render_mcp_json

from .test_provider_codex import profile


def job(job_id: int = 3) -> ReviewJob:
    return ReviewJob(
        id=job_id,
        repo="alvaro/homelab",
        project="homelab",
        provider="claude-work",
        pr_number=8,
        head_sha="head",
        base_sha="base",
        status=JobStatus.RUNNING,
        attempts=1,
        queued_at=1.0,
        started_at=2.0,
        finished_at=None,
        last_error=None,
    )


def _provider(
    tmp_path: Path,
    raw: MagicMock,
    account: str = "claude-work",
    token: str = "sk-ant-oat01-x",
    credential_env: ClaudeCredentialEnv = "CLAUDE_CODE_OAUTH_TOKEN",
):
    return ClaudeReviewProvider(
        account=account,
        credential_env=credential_env,
        credential=token,
        docker_client=raw,
        projects_root=tmp_path / "projects",
        router_url="http://claude-router:8000",
        model="claude-opus-5",
    )


def _docker(container_name: str = "claude-review-3-claude-work") -> MagicMock:
    raw = MagicMock()
    image = MagicMock()
    image.id = "sha256:image"
    raw.images.get.return_value = image
    container = MagicMock()
    container.name = container_name
    raw.containers.run.return_value = container
    return raw


def test_starts_a_claude_review_container(tmp_path: Path) -> None:
    raw = _docker()
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    session = _provider(tmp_path, raw).start_review_session(job(), worktree, profile())

    raw.images.get.assert_called_once_with("homelab/claude-code-homelab:latest")
    assert session.container == "claude-review-3-claude-work"
    env = raw.containers.run.call_args.kwargs["environment"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-x"
    assert env["GITHUB_REPO"] == "alvaro/homelab"


def test_review_container_cannot_post_to_github(tmp_path: Path) -> None:
    """The worker is the sole poster.

    The overlay's `gh` wrapper reads CLAUDE_ROLE — not AGENT_ROLE, which is
    codex's name for it — so a copy-paste from the codex provider would hand
    the model a write-capable token and every PR would get two reviews.
    """
    raw = _docker()
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    _provider(tmp_path, raw).start_review_session(job(), worktree, profile())

    env = raw.containers.run.call_args.kwargs["environment"]
    assert env["CLAUDE_ROLE"] == agent_container.REVIEWER_GH_ROLE
    assert env["CLAUDE_ROLE"] == "reviewer-readonly"


def test_the_two_accounts_never_share_config_state(tmp_path: Path) -> None:
    """Two subscriptions under one CLAUDE_CONFIG_DIR make the CLI reuse the
    first account's cached credentials, which silently spends the wrong
    window — the failover would then hit the same exhausted quota it fled."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    dirs = []
    for account in ("claude-work", "claude-personal"):
        raw = _docker(f"claude-review-3-{account}")
        _provider(tmp_path, raw, account=account).start_review_session(job(), worktree, profile())
        volumes = raw.containers.run.call_args.kwargs["volumes"]
        dirs.append(next(h for h, spec in volumes.items() if spec["bind"] == "/home/agent/.claude"))
        assert raw.containers.run.call_args.kwargs["name"].endswith(account)

    assert dirs[0] != dirs[1], "each account needs its own config dir"
    assert "claude-work" in dirs[0] and "claude-personal" in dirs[1]


def test_an_api_key_account_runs_on_the_key_alone(tmp_path: Path) -> None:
    """Monthly API credits pay only for runs signed in with a Console key. An
    OAuth token beside it could put the review on the plan instead, leaving the
    credits to expire while the account still reads as `claude-api`."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw = _docker("claude-review-3-claude-api")

    _provider(
        tmp_path,
        raw,
        account="claude-api",
        token="sk-ant-api03-x",
        credential_env="ANTHROPIC_API_KEY",
    ).start_review_session(job(), worktree, profile())

    environment = raw.containers.run.call_args.kwargs["environment"]
    assert environment["ANTHROPIC_API_KEY"] == "sk-ant-api03-x"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in environment


def test_a_plan_account_never_receives_an_api_key(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw = _docker()

    _provider(tmp_path, raw).start_review_session(job(), worktree, profile())

    environment = raw.containers.run.call_args.kwargs["environment"]
    assert environment["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-x"
    assert "ANTHROPIC_API_KEY" not in environment


def test_spent_api_credits_are_an_outage(tmp_path: Path) -> None:
    """The credits run out by design every month; that must move the chain on."""
    payload = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "result": "Credit balance is too low",
        }
    )

    with pytest.raises(ProviderUnavailable) as caught:
        _run(tmp_path, payload, exit_code=1)

    assert caught.value.reason == "exhausted"


def test_missing_token_is_an_outage_not_a_crash(tmp_path: Path) -> None:
    """An unconfigured second account must fail over past, not fail the PR."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    provider = _provider(tmp_path, _docker(), account="claude-personal", token="")

    with pytest.raises(ProviderUnavailable) as caught:
        provider.start_review_session(job(), worktree, profile())

    assert caught.value.reason == "auth"
    assert caught.value.provider == "claude-personal"


def _run(tmp_path: Path, output: str, exit_code: int = 0):
    raw = _docker()
    container = MagicMock()
    container.exec_run.return_value = MagicMock(exit_code=exit_code, output=output.encode())
    raw.containers.get.return_value = container
    provider = _provider(tmp_path, raw)
    return provider.run_review(SessionRef(id="3", container="c", worktree=tmp_path), "p", 60)


def test_parses_the_verdict_out_of_output_format_json(tmp_path: Path) -> None:
    payload = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Looks fine.\n\nVERDICT: APPROVE",
        }
    )

    result = _run(tmp_path, payload)

    assert result.event == "APPROVE"
    assert result.body == "Looks fine."


def test_a_missing_verdict_never_approves(tmp_path: Path) -> None:
    payload = json.dumps({"type": "result", "is_error": False, "result": "Some prose."})

    assert _run(tmp_path, payload).event == "REQUEST_CHANGES"


def test_its_own_usage_limit_is_an_outage(tmp_path: Path) -> None:
    """The fallback can be exhausted too — that must advance the chain to the
    second account rather than fail the PR."""
    payload = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "result": "Claude AI usage limit reached|1788400000",
        }
    )

    with pytest.raises(ProviderUnavailable) as caught:
        _run(tmp_path, payload, exit_code=1)

    assert caught.value.reason == "exhausted"


def test_a_non_json_failure_is_still_classified(tmp_path: Path) -> None:
    """The CLI can die before emitting any JSON at all. Handing the raw text to
    the classifier is what keeps that from being an opaque job failure."""
    with pytest.raises(ProviderUnavailable) as caught:
        _run(tmp_path, "error: invalid token provided\n", exit_code=1)

    assert caught.value.reason == "auth"


def test_mcp_json_uses_the_transport_key_claude_actually_reads() -> None:
    """`transport` instead of `type` yields a valid-looking config that claude
    treats as having no servers — a reviewer that silently cannot see GitHub."""
    body = json.loads(render_mcp_json(caller="claude-code-server-homelab", mcps={"github": ["ro"]}))

    assert body["mcpServers"]["github"]["type"] == "http"
    assert body["mcpServers"]["github"]["headers"]["X-MCP-Caller"] == "claude-code-server-homelab"


def test_a_review_body_quoting_a_quota_message_is_not_an_outage(tmp_path: Path) -> None:
    """Same leak as the codex side: only error text may be classified.

    Here `is_error` is false, so `result` holds the review itself — and a
    review of a rate-limiting PR quotes the whole signature table.
    """
    payload = json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": "The diff handles 'You've hit your usage limit'.\n\nVERDICT: APPROVE",
        }
    )

    with pytest.raises(RuntimeError) as caught:
        _run(tmp_path, payload, exit_code=1)

    assert not isinstance(caught.value, ProviderUnavailable)


def test_mcp_config_is_written_into_the_mounted_config_dir(tmp_path: Path) -> None:
    """Rendered where the CLI is pointed at it, and never into the worktree.

    claude does not auto-discover this path — run_review passes --mcp-config —
    which is exactly what lets it live outside ground the PR controls.
    """
    raw = _docker()
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    _provider(tmp_path, raw).start_review_session(job(), worktree, profile())

    volumes = raw.containers.run.call_args.kwargs["volumes"]
    host_config = next(h for h, spec in volumes.items() if spec["bind"] == "/home/agent/.claude")
    written = Path(host_config) / ".mcp.json"
    assert written.is_file()
    assert json.loads(written.read_text())["mcpServers"]
    assert not (worktree / ".claude").exists(), "nothing may be written into the PR's worktree"


def test_a_hostile_dot_claude_in_the_worktree_cannot_be_written_through(
    tmp_path: Path,
) -> None:
    """The worktree is a checkout of the PR head and this service runs as root
    outside the container. A branch committing `.claude` as a symlink used to
    get this write to land wherever the link pointed."""
    raw = _docker()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (worktree / ".claude").symlink_to(target)

    _provider(tmp_path, raw).start_review_session(job(), worktree, profile())

    assert not (target / ".mcp.json").exists(), "must not write through a branch-supplied symlink"


def test_a_dot_claude_file_in_the_worktree_does_not_break_the_review(
    tmp_path: Path,
) -> None:
    """Committed as a regular file it used to raise FileExistsError, which is
    not ProviderUnavailable — so the job failed without failing over, turning a
    hostile (or merely odd) branch into a red required check."""
    raw = _docker()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".claude").write_text("not a directory")

    session = _provider(tmp_path, raw).start_review_session(job(), worktree, profile())

    assert session.container.endswith("claude-work")


def test_the_cli_is_locked_to_that_one_config(tmp_path: Path) -> None:
    """--strict-mcp-config, because the reviewer runs inside the PR's own
    worktree with --dangerously-skip-permissions: without it, an .mcp.json
    shipped by the branch under review would be honoured."""
    raw = _docker()
    container = MagicMock()
    ok = {"type": "result", "is_error": False, "result": "VERDICT: APPROVE"}
    container.exec_run.return_value = MagicMock(exit_code=0, output=json.dumps(ok).encode())
    raw.containers.get.return_value = container

    _provider(tmp_path, raw).run_review(
        SessionRef(id="3", container="c", worktree=tmp_path), "p", 60
    )

    cmd = container.exec_run.call_args.args[0]
    assert "--mcp-config" in cmd
    assert cmd[cmd.index("--mcp-config") + 1] == "/home/agent/.claude/.mcp.json"
    assert "--strict-mcp-config" in cmd


def test_the_branch_under_review_brings_no_settings_or_hooks(tmp_path: Path) -> None:
    """Project settings come from the PR's worktree. Their hooks would run next to
    the OAuth token, and ordago's SessionStart hook added ~20k characters that
    every later turn re-read (measured 2026-10-20)."""
    raw = _docker()
    container = MagicMock()
    ok = {"type": "result", "is_error": False, "result": "VERDICT: APPROVE"}
    container.exec_run.return_value = MagicMock(exit_code=0, output=json.dumps(ok).encode())
    raw.containers.get.return_value = container

    _provider(tmp_path, raw).run_review(
        SessionRef(id="3", container="c", worktree=tmp_path), "p", 60
    )

    cmd = container.exec_run.call_args.args[0]
    assert cmd[cmd.index("--setting-sources") + 1] == "user"


def test_git_works_in_the_container_but_cannot_write_the_clone(tmp_path: Path) -> None:
    raw = _docker()
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    _provider(tmp_path, raw).start_review_session(job(), worktree, profile())

    git_dir = str(tmp_path / "projects" / "homelab" / "repo" / ".git")
    volumes = raw.containers.run.call_args.kwargs["volumes"]
    assert volumes[git_dir] == {"bind": git_dir, "mode": "ro"}


# ── quota capture ─────────────────────────────────────────────────────────────
# The two payloads below are verbatim `rate_limit_info` objects read off the two
# live subscriptions on 2026-09-04, and they are recorded rather than invented
# because their *difference* is the whole design constraint: at the same moment
# one account reported an `overage` limit with a number, and the other a
# `five_hour` limit with no number at all.

WORK_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed_warning",
        "resetsAt": 1790812800,
        "rateLimitType": "overage",
        "utilization": 18.67,
        "isUsingOverage": False,
        "surpassedThreshold": 1,
    },
}
PERSONAL_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "resetsAt": 1788516000,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled",
        "isUsingOverage": False,
    },
}
RESULT_EVENT = {"type": "result", "is_error": False, "result": "VERDICT: APPROVE"}


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events)


def _provider_after(tmp_path: Path, output: str, exit_code: int = 0):
    """Run one review and hand back the provider, so its reading can be read."""
    raw = _docker()
    container = MagicMock()
    container.exec_run.return_value = MagicMock(exit_code=exit_code, output=output.encode())
    raw.containers.get.return_value = container
    provider = _provider(tmp_path, raw)
    # Suppressed: an exhausted account raises, and its quota is exactly what the
    # caller is here to inspect.
    with contextlib.suppress(Exception):
        provider.run_review(SessionRef(id="3", container="c", worktree=tmp_path), "p", 60)
    return provider


def test_the_review_stream_carries_the_rate_limit_event(tmp_path: Path) -> None:
    """--output-format json has no rate-limit field at all, so the reviewer asks
    for the stream. If this flag regresses, the quota card silently empties."""
    raw = _docker()
    container = MagicMock()
    container.exec_run.return_value = MagicMock(exit_code=0, output=_stream(RESULT_EVENT).encode())
    raw.containers.get.return_value = container

    _provider(tmp_path, raw).run_review(
        SessionRef(id="3", container="c", worktree=tmp_path), "p", 60
    )

    cmd = container.exec_run.call_args.args[0]
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    # stream-json refuses to run without it.
    assert "--verbose" in cmd


def test_quota_is_captured_from_a_normal_review(tmp_path: Path) -> None:
    provider = _provider_after(tmp_path, _stream(WORK_EVENT, RESULT_EVENT))

    reading = provider.last_quota
    assert reading is not None
    assert reading.account == "claude-work"
    assert reading.utilization == 18.67
    assert reading.limit_type == "overage"
    assert reading.status == "allowed_warning"
    assert reading.resets_at == 1790812800


def test_no_utilization_reported_is_not_zero_percent(tmp_path: Path) -> None:
    """The CLI omits `utilization` until a threshold is surpassed. Coercing the
    absence to 0.0 would render the healthiest account as the freshest-reset
    one — a number nobody measured, in the one place the operator looks to
    decide whether the reviewer can run tonight."""
    provider = _provider_after(tmp_path, _stream(PERSONAL_EVENT, RESULT_EVENT))

    reading = provider.last_quota
    assert reading is not None
    assert reading.utilization is None
    assert reading.limit_type == "five_hour"
    assert reading.status == "allowed"


def test_quota_is_captured_even_when_the_account_is_exhausted(tmp_path: Path) -> None:
    """An exhausted account is exactly when its quota is worth reporting, and
    that run raises. Capture must happen before the raise, not after it."""
    spent = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "result": "Claude AI usage limit reached|1788516000",
        }
    )
    provider = _provider_after(tmp_path, _stream(WORK_EVENT) + "\n" + spent, exit_code=1)

    assert provider.last_quota is not None
    assert provider.last_quota.utilization == 18.67


def test_a_review_with_no_rate_limit_event_keeps_the_previous_reading(
    tmp_path: Path,
) -> None:
    """Losing a quota number must never cost a review, and a stream that omits
    the event says nothing new — not that the account is unmeasured."""
    raw = _docker()
    container = MagicMock()
    raw.containers.get.return_value = container
    provider = _provider(tmp_path, raw)
    session = SessionRef(id="3", container="c", worktree=tmp_path)

    container.exec_run.return_value = MagicMock(
        exit_code=0, output=_stream(WORK_EVENT, RESULT_EVENT).encode()
    )
    provider.run_review(session, "p", 60)
    container.exec_run.return_value = MagicMock(exit_code=0, output=_stream(RESULT_EVENT).encode())
    provider.run_review(session, "p", 60)

    assert provider.last_quota is not None
    assert provider.last_quota.utilization == 18.67


def test_an_unparseable_stream_never_breaks_the_review(tmp_path: Path) -> None:
    noise = "not json at all\n" + json.dumps({"type": "rate_limit_event"}) + "\n"
    provider = _provider_after(tmp_path, noise + _stream(RESULT_EVENT))

    assert provider.last_quota is None


def test_verbose_stream_events_are_never_shown_to_the_classifier(tmp_path: Path) -> None:
    """`--verbose` is the price of the stream, and it puts the model's prose and
    every tool call into the same output the classifier reads on failure.

    A review of a rate-limiting PR quotes the whole signature table, so if any
    of those events reached `classify_unavailable` this run would be reported as
    an exhausted subscription — failing the chain over to the spare account on a
    review that worked. Safe only because `diagnostic_lines` drops every line
    that parses as JSON; this pins that, because the exposure is new.
    """
    quota_prose = "You've hit your usage limit. Upgrade to Pro or try again at 1:43 AM."
    assistant = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": quota_prose}]}}
    )
    tool_use = json.dumps(
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": quota_prose}]}}
    )
    failed = json.dumps({"type": "result", "is_error": True, "result": "container exec returned 1"})

    with pytest.raises(RuntimeError) as caught:
        _run(tmp_path, "\n".join([assistant, tool_use, failed]), exit_code=1)

    assert not isinstance(caught.value, ProviderUnavailable)


def test_a_real_outage_on_plain_stderr_is_still_classified_through_the_stream(
    tmp_path: Path,
) -> None:
    """The other half of the same rule: a CLI failure that arrives as plain text
    interleaved with the events must still advance the chain. Without this, the
    verbose test above would pass just as well for a classifier that had stopped
    working entirely."""
    stream = json.dumps({"type": "system", "subtype": "init"})
    stderr = "Claude AI usage limit reached. Your limit will reset at 3:31 AM."
    failed = json.dumps({"type": "result", "is_error": True, "result": ""})

    with pytest.raises(ProviderUnavailable) as caught:
        _run(tmp_path, "\n".join([stream, stderr, failed]), exit_code=1)

    assert caught.value.provider == "claude-work"


def test_a_large_prompt_reaches_claude_on_stdin_not_argv(tmp_path: Path) -> None:
    """Regression: the prompt embeds the whole diff, and as one argv element it
    failed every PR past the kernel's 128 KiB single-argument cap."""
    import io
    import tarfile

    raw = _docker()
    container = MagicMock()
    ok = {"type": "result", "is_error": False, "result": "VERDICT: APPROVE"}
    container.exec_run.return_value = MagicMock(exit_code=0, output=json.dumps(ok).encode())
    raw.containers.get.return_value = container
    prompt = "x" * 300_000

    _provider(tmp_path, raw).run_review(
        SessionRef(id="3", container="c", worktree=tmp_path), prompt, 60
    )

    cmd = container.exec_run.call_args.args[0]
    assert all(len(arg) < 1_000 for arg in cmd)
    assert cmd[:5] == ["sh", "-c", f'exec "$@" < {PROMPT_PATH}', "sh", "claude"]
    assert cmd[5] == "-p"
    _, archive = container.put_archive.call_args.args
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        extracted = tar.extractfile(Path(PROMPT_PATH).name)
        assert extracted is not None
        assert extracted.read().decode("utf-8") == prompt
