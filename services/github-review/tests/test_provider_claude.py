"""Every Claude account, and the hardened container each one reviews in.

One provider instance per account: the point of a second Anthropic account is
that it has its own quota, so two must never share credentials, container
names, or state. And whatever the account, its credential sits in a container
reading a checkout the PR's author wrote — so most of what is pinned here is
that the checkout cannot reach the credential or configure the CLI.
"""

import contextlib
import io
import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from docker.errors import ImageNotFound
from src.config import ClaudeAccount, CredentialSource
from src.job_store import JobStatus, ReviewJob
from src.provider import (
    ProviderUnavailable,
    ReviewFailed,
    ReviewLimitExceeded,
    SessionRef,
    run_cost,
)
from src.providers._agent_container import INSTANCE_LABEL, PROMPT_PATH
from src.providers.claude import DIFF_PATH, ClaudeReviewProvider, review_prompt

TIMEOUT = 30 * 60
IMAGE = "ordago-ci/claude-review:test"
DIFF = "diff --git a/a.py b/a.py\n"


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


def _settings(auth: str = "subscription", env: str = "WORK_TOKEN") -> ClaudeAccount:
    return ClaudeAccount(
        kind="claude",
        auth=auth,  # type: ignore[arg-type]
        credential=CredentialSource(env=env),
        model="claude-model",
        max_budget_usd=3.0 if auth == "api_key" else None,
    )


def _docker() -> MagicMock:
    raw = MagicMock()
    raw.images.get.return_value = MagicMock(id="sha256:image")
    container = MagicMock()
    raw.containers.run.return_value = container
    raw.containers.get.return_value = container
    return raw


def _provider(
    tmp_path: Path,
    raw: MagicMock | None = None,
    *,
    account: str = "claude-work",
    settings: ClaudeAccount | None = None,
    environ: dict[str, str] | None = None,
) -> ClaudeReviewProvider:
    worktrees = MagicMock()
    worktrees.pr_diff.return_value = DIFF
    return ClaudeReviewProvider(
        account=account,
        settings=settings or _settings(),
        docker_client=raw if raw is not None else _docker(),
        worktrees=worktrees,
        projects_root=tmp_path / "projects",
        image=IMAGE,
        instance="test",
        environ=environ if environ is not None else {"WORK_TOKEN": "sk-ant-oat01-x"},
    )


def _worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    return worktree


def _archived_files(container: MagicMock) -> dict[str, str]:
    files: dict[str, str] = {}
    for call in container.put_archive.call_args_list:
        target_dir, archive = call.args
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar.getmembers():
                extracted = tar.extractfile(member)
                assert extracted is not None
                files[f"{target_dir}/{member.name}"] = extracted.read().decode()
    return files


def _run(tmp_path: Path, output: str, exit_code: int = 0, provider=None):
    raw = _docker()
    raw.containers.get.return_value.exec_run.return_value = MagicMock(
        exit_code=exit_code, output=output.encode()
    )
    provider = provider or _provider(tmp_path, raw)
    return provider.run_review(SessionRef(id="3", container="c", worktree=tmp_path), "p", 60)


def _result(text: str = "", **fields: object) -> str:
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": text, **fields}
    )


# ── the container ──────────────────────────────────────────────────────────


def test_the_container_is_the_sandbox_and_the_checkout_is_read_only(tmp_path: Path) -> None:
    raw = _docker()
    worktree = _worktree(tmp_path)

    session = _provider(tmp_path, raw).start_review_session(job(), worktree, TIMEOUT)

    run = raw.containers.run.call_args.kwargs
    assert session.container == "review-test-claude-work-3"
    assert run["name"] == session.container
    assert run["image"] == "sha256:image"
    raw.images.get.assert_called_once_with(IMAGE)
    # The checkout only, read-only: no clone .git, no config dir, nothing writable.
    assert run["volumes"] == {str(worktree): {"bind": "/workspace", "mode": "ro"}}
    assert run["working_dir"] == "/workspace"
    assert run["user"] == "1000:1000"
    assert run["cap_drop"] == ["ALL"]
    assert run["security_opt"] == ["no-new-privileges"]
    # Off this service's network: nothing there for the PR's code to reach.
    assert run["network"] == "bridge"
    assert run["labels"] == {INSTANCE_LABEL: "test"}


def test_a_subscription_runs_on_its_oauth_token_alone(tmp_path: Path) -> None:
    raw = _docker()
    _provider(tmp_path, raw).start_review_session(job(), _worktree(tmp_path), TIMEOUT)

    assert raw.containers.run.call_args.kwargs["environment"] == {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"
    }


def test_an_api_key_account_runs_on_the_key_alone(tmp_path: Path) -> None:
    """Both set, the CLI picks one by its own precedence and the wrong account pays."""
    raw = _docker()
    provider = _provider(
        tmp_path,
        raw,
        account="claude-api",
        settings=_settings("api_key", env="REVIEW_KEY"),
        environ={"REVIEW_KEY": "sk-ant-api03-y", "WORK_TOKEN": "sk-ant-oat01-x"},
    )

    provider.start_review_session(job(), _worktree(tmp_path), TIMEOUT)

    assert raw.containers.run.call_args.kwargs["environment"] == {
        "ANTHROPIC_API_KEY": "sk-ant-api03-y"
    }


def test_a_credential_can_be_read_from_a_file(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-ant-api03-from-file\n")
    raw = _docker()
    settings = ClaudeAccount(
        kind="claude",
        auth="api_key",
        credential=CredentialSource(file=str(key)),
        model="m",
        max_budget_usd=1,
    )

    _provider(tmp_path, raw, settings=settings).start_review_session(
        job(), _worktree(tmp_path), TIMEOUT
    )

    environment = raw.containers.run.call_args.kwargs["environment"]
    assert environment == {"ANTHROPIC_API_KEY": "sk-ant-api03-from-file"}


@pytest.mark.parametrize("environ", [{}, {"WORK_TOKEN": "  "}])
def test_a_missing_credential_is_an_outage_not_a_crash(
    tmp_path: Path, environ: dict[str, str]
) -> None:
    """An unconfigured spare account must fail over past, not fail the PR."""
    raw = _docker()
    provider = _provider(tmp_path, raw, account="claude-personal", environ=environ)

    with pytest.raises(ProviderUnavailable) as caught:
        provider.start_review_session(job(), _worktree(tmp_path), TIMEOUT)

    assert (caught.value.provider, caught.value.reason) == ("claude-personal", "auth")
    assert "WORK_TOKEN" in caught.value.detail
    raw.containers.run.assert_not_called()


def test_an_unbuilt_image_is_an_outage(tmp_path: Path) -> None:
    raw = _docker()
    raw.images.get.side_effect = ImageNotFound("no such image")

    with pytest.raises(ProviderUnavailable) as caught:
        _provider(tmp_path, raw).start_review_session(job(), _worktree(tmp_path), TIMEOUT)

    assert caught.value.reason == "tooling"


def test_the_two_accounts_never_share_a_container(tmp_path: Path) -> None:
    raw = _docker()
    environ = {"WORK_TOKEN": "a", "PERSONAL_TOKEN": "b"}
    work = _provider(tmp_path, raw, environ=environ)
    personal = _provider(
        tmp_path,
        raw,
        account="claude-personal",
        settings=_settings(env="PERSONAL_TOKEN"),
        environ=environ,
    )

    names = {
        work.start_review_session(job(), _worktree(tmp_path), TIMEOUT).container,
        personal.start_review_session(job(), _worktree(tmp_path), TIMEOUT).container,
    }

    assert names == {"review-test-claude-work-3", "review-test-claude-personal-3"}


def test_the_pr_diff_goes_into_the_container_where_the_prompt_says(tmp_path: Path) -> None:
    raw = _docker()
    provider = _provider(tmp_path, raw)

    provider.start_review_session(job(), _worktree(tmp_path), TIMEOUT)

    assert _archived_files(raw.containers.run.return_value)[DIFF_PATH] == DIFF
    provider._worktrees.pr_diff.assert_called_once_with(  # type: ignore[attr-defined]
        repo_dir=tmp_path / "projects" / "homelab" / "repo", base_sha="base", head_sha="head"
    )
    assert DIFF_PATH in review_prompt("the prompt")
    assert review_prompt("the prompt").endswith("the prompt")


def test_a_symlink_committed_by_the_pr_is_never_written_through(tmp_path: Path) -> None:
    # Regression: the diff used to be written into the worktree, by a service running
    # as root, at a path the PR could commit as a symlink to anywhere on the host.
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    worktree = _worktree(tmp_path)
    for name in (Path(DIFF_PATH).name, ".pr-review.diff", ".claude"):
        (worktree / name).symlink_to(victim)

    _provider(tmp_path).start_review_session(job(), worktree, TIMEOUT)

    assert victim.read_text() == "untouched"


def test_a_failed_diff_copy_leaves_no_container_behind(tmp_path: Path) -> None:
    raw = _docker()
    container = raw.containers.run.return_value
    container.put_archive.side_effect = RuntimeError("copy")

    with pytest.raises(RuntimeError, match="copy"):
        _provider(tmp_path, raw).start_review_session(job(), _worktree(tmp_path), TIMEOUT)

    container.remove.assert_called_with(force=True)


def test_no_diff_is_a_job_failure_not_an_outage(tmp_path: Path) -> None:
    raw = _docker()
    provider = _provider(tmp_path, raw)
    provider._worktrees.pr_diff.return_value = None  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError) as caught:
        provider.start_review_session(job(), _worktree(tmp_path), TIMEOUT)

    assert not isinstance(caught.value, ProviderUnavailable)
    raw.containers.run.assert_not_called()


# ── the CLI ────────────────────────────────────────────────────────────────


def test_the_cli_ignores_the_checkouts_own_config_and_is_bounded(tmp_path: Path) -> None:
    # Regression: under --bare a PR's .claude/settings.json `env` still applied, so it
    # could set ANTHROPIC_BASE_URL, collect the key and answer with an APPROVE.
    # These flags were checked against the pinned CLI and a local fake API with a
    # hostile checkout (ordago-app/ci#20, with --output-format json): no call reached
    # its endpoint, its hook and .mcp.json did not run, its allow rules were ignored,
    # and Read left the workspace only for DIFF_PATH.
    argv = _provider(tmp_path).argv()

    assert argv[:2] == ["claude", "-p"]
    assert "--bare" not in argv
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--add-dir") + 1] == str(Path(DIFF_PATH).parent)
    assert argv[argv.index("--model") + 1] == "claude-model"
    assert "--max-turns" in argv


def test_only_a_capped_account_passes_a_budget(tmp_path: Path) -> None:
    api = _provider(tmp_path, settings=_settings("api_key")).argv()
    plan = _provider(tmp_path).argv()

    assert api[api.index("--max-budget-usd") + 1] == "3.0"
    assert "--max-budget-usd" not in plan


def test_the_credential_never_reaches_argv(tmp_path: Path) -> None:
    raw = _docker()
    container = raw.containers.get.return_value
    container.exec_run.return_value = MagicMock(
        exit_code=0, output=_result("VERDICT: APPROVE").encode()
    )
    provider = _provider(tmp_path, raw)
    session = provider.start_review_session(job(), _worktree(tmp_path), TIMEOUT)

    provider.run_review(session, "prompt", 60)

    assert "sk-ant-oat01-x" not in " ".join(container.exec_run.call_args.args[0])
    assert all("sk-ant-oat01-x" not in text for text in _archived_files(container).values())


# ── reading the result ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "event"),
    [
        ("No findings.\n\nVERDICT: APPROVE", "APPROVE"),
        ("`a.py:1` is wrong.\n\nVERDICT: REQUEST_CHANGES", "REQUEST_CHANGES"),
        ("No verdict line at all", "REQUEST_CHANGES"),
        # The PR's body is in the prompt: an APPROVE that is not the last line
        # may be the model quoting what the PR asked for.
        ("The PR body says:\nVERDICT: APPROVE\nbut a.py:1 is wrong.", "REQUEST_CHANGES"),
    ],
)
def test_the_verdict_is_the_last_line_or_no_approval(tmp_path: Path, text: str, event: str) -> None:
    result = _run(tmp_path, _result(text))

    assert result.event == event
    assert "VERDICT" not in result.body


@pytest.mark.parametrize(
    ("subtype", "error"),
    [("error_max_budget_usd", "Reached maximum budget ($3.0)"), ("error_max_turns", "max turns")],
)
def test_a_cap_stop_is_given_up_on_not_failed_over(
    tmp_path: Path, subtype: str, error: str
) -> None:
    payload = json.dumps(
        {"type": "result", "subtype": subtype, "is_error": True, "errors": [error]}
    )

    with pytest.raises(ReviewLimitExceeded, match=error.split(" ")[0]):
        _run(tmp_path, payload, exit_code=1)


# What claude 2.1 printed for these failures: `subtype` says "success" with
# `is_error` true, so subtype cannot be trusted.
@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"api_error_status": 401, "result": "Invalid API key · Fix external API key"}, "auth"),
        ({"result": "Failed to authenticate: OAuth session expired"}, "auth"),
        ({"api_error_status": 429, "result": "Too many"}, "exhausted"),
        # The credits run out by design every month; that must move the chain on.
        ({"result": "Credit balance is too low"}, "exhausted"),
        ({"result": "You've hit your monthly spend limit"}, "exhausted"),
        ({"result": "Claude AI usage limit reached|1788400000"}, "exhausted"),
    ],
)
def test_auth_and_credit_failures_are_outages(tmp_path: Path, payload: dict, reason: str) -> None:
    event = {"type": "result", "subtype": "success", "is_error": True, **payload}

    with pytest.raises(ProviderUnavailable) as caught:
        _run(tmp_path, json.dumps(event), exit_code=1)

    assert (caught.value.provider, caught.value.reason) == ("claude-work", reason)


@pytest.mark.parametrize(
    "event",
    [
        {"type": "result", "subtype": "error_during_execution", "is_error": False},
        {"type": "result", "subtype": "success", "is_error": False, "result": "   "},
    ],
)
def test_other_failures_are_plain_errors_for_the_worker_to_retry(
    tmp_path: Path, event: dict
) -> None:
    with pytest.raises(RuntimeError) as caught:
        _run(tmp_path, json.dumps(event))

    assert not isinstance(caught.value, ProviderUnavailable)


def test_a_non_json_failure_is_still_classified(tmp_path: Path) -> None:
    """The CLI can die before emitting any JSON at all. Handing the raw text to
    the classifier is what keeps that from being an opaque job failure."""
    with pytest.raises(ProviderUnavailable) as caught:
        _run(tmp_path, "error: invalid token provided\n", exit_code=1)

    assert caught.value.reason == "auth"


def test_an_unclassified_non_json_failure_surfaces_its_output(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="DISTINCTIVE_MARKER_42"):
        _run(tmp_path, "DISTINCTIVE_MARKER_42\n", exit_code=1)


def test_a_review_body_quoting_a_quota_message_is_not_an_outage(tmp_path: Path) -> None:
    """Only error text may be classified. Here `is_error` is false, so `result`
    holds the review itself — and a review of a rate-limiting PR quotes the
    whole signature table."""
    body = _result("The diff handles 'You've hit your usage limit'.\n\nVERDICT: APPROVE")

    with pytest.raises(RuntimeError) as caught:
        _run(tmp_path, body, exit_code=1)

    assert not isinstance(caught.value, ProviderUnavailable)


def test_cleanup_and_discard_remove_the_container(tmp_path: Path) -> None:
    raw = _docker()
    provider = _provider(tmp_path, raw)

    provider.cleanup(SessionRef(id="3", container="review-test-claude-work-3", worktree=tmp_path))
    provider.discard(job())

    assert raw.containers.get.call_args_list[-1].args == ("review-test-claude-work-3",)
    assert raw.containers.get.return_value.remove.call_count == 2


# ── quota ──────────────────────────────────────────────────────────────────


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
RESULT_EVENT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "VERDICT: APPROVE",
}


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
    raw = _docker()
    container = MagicMock()
    container.exec_run.return_value = MagicMock(exit_code=0, output=_stream(RESULT_EVENT).encode())
    raw.containers.get.return_value = container
    prompt = "x" * 300_000

    _provider(tmp_path, raw).run_review(
        SessionRef(id="3", container="c", worktree=tmp_path), prompt, 60
    )

    cmd = container.exec_run.call_args.args[0]
    assert all(len(arg) < 1_000 for arg in cmd)
    assert cmd[:5] == ["sh", "-c", f'exec "$@" < {PROMPT_PATH}', "sh", "claude"]
    assert cmd[5] == "-p"
    assert _archived_files(container)[PROMPT_PATH] == review_prompt(prompt)


def test_a_stream_cut_short_is_classified_by_its_diagnostics_only(tmp_path: Path) -> None:
    """With no result event the CLI died mid-stream, and everything it printed
    before that is the transcript: the model quoting a rate-limiting PR must not
    turn the crash into an outage that moves the chain onto the spare account."""
    quota_prose = "You've hit your usage limit. Upgrade to Pro or try again at 1:43 AM."
    assistant = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": quota_prose}]}}
    )

    with pytest.raises(RuntimeError) as caught:
        _run(tmp_path, assistant + "\nSegmentation fault\n", exit_code=139)

    assert not isinstance(caught.value, ProviderUnavailable)


@pytest.mark.parametrize(
    ("output", "spent"),
    [
        (json.dumps({"type": "result", "is_error": True, "api_error_status": 429}), False),
        (
            json.dumps(
                {"type": "result", "is_error": True, "api_error_status": 429, "num_turns": 4}
            ),
            True,
        ),
        (
            json.dumps({"type": "assistant", "message": {"content": []}})
            + "\nClaude AI usage limit reached. Your limit will reset at 3:31 AM.\n",
            True,
        ),
        ("error: invalid token provided\n", False),
    ],
)
def test_an_outage_says_whether_the_model_already_ran(
    tmp_path: Path, output: str, spent: bool
) -> None:
    """An account that gave out before the first turn cost nothing, so the run
    cap must not count it, or an outage would use up a PR's runs. One that gave
    out mid-review was paid for, and the cap must count it."""
    with pytest.raises(ProviderUnavailable) as caught:
        _run(tmp_path, output, exit_code=1)

    assert caught.value.read_diff is spent


# ── cost ───────────────────────────────────────────────────────────────────


def test_a_review_carries_the_cost_the_cli_reported(tmp_path: Path) -> None:
    result = _run(tmp_path, _result("No findings.\nVERDICT: APPROVE", total_cost_usd=0.4213))

    assert result.event == "APPROVE"
    assert result.cost_usd == pytest.approx(0.4213)


def test_a_run_that_reports_no_cost_is_unknown_not_free(tmp_path: Path) -> None:
    assert _run(tmp_path, _result("No findings.\nVERDICT: APPROVE")).cost_usd is None


def test_every_paid_failure_still_carries_its_cost(tmp_path: Path) -> None:
    """A run that read the diff and then failed was paid for, so its cost must
    reach the ledger whichever way it failed."""
    capped = json.dumps(
        {
            "type": "result",
            "subtype": "error_max_budget_usd",
            "is_error": True,
            "errors": ["Reached maximum budget ($3.0)"],
            "total_cost_usd": 3.02,
        }
    )
    with pytest.raises(ReviewLimitExceeded) as limit:
        _run(tmp_path, capped, exit_code=1)
    assert run_cost(limit.value) == pytest.approx(3.02)

    exhausted = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": 429,
            "result": "Too many",
            "num_turns": 4,
            "total_cost_usd": 0.8,
        }
    )
    with pytest.raises(ProviderUnavailable) as outage:
        _run(tmp_path, exhausted, exit_code=1)
    assert run_cost(outage.value) == pytest.approx(0.8)

    broken = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "boom",
            "total_cost_usd": 0.1,
        }
    )
    with pytest.raises(ReviewFailed) as failed:
        _run(tmp_path, broken, exit_code=1)
    assert run_cost(failed.value) == pytest.approx(0.1)
