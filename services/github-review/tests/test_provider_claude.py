import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from docker.errors import ImageNotFound
from src.job_store import JobStatus, ReviewJob
from src.provider import ProviderUnavailableError, SessionRef
from src.providers import claude as claude_mod
from src.providers.claude import ClaudeReviewProvider

from .test_provider_codex import profile

SESSION = SessionRef(id="3", container="claude-review-3", worktree=Path("/w"))


def job() -> ReviewJob:
    return ReviewJob(
        id=3,
        repo="alvaro/homelab",
        project="homelab",
        provider="codex",
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


def _provider(tmp_path: Path, *, key: str | None = "sk-test-key") -> ClaudeReviewProvider:
    raw = MagicMock()
    raw.images.get.return_value = MagicMock(id="sha256:image")
    container = MagicMock()
    container.name = "claude-review-3"
    raw.containers.run.return_value = container
    raw.containers.get.return_value = container
    worktrees = MagicMock()
    worktrees.pr_diff.return_value = "diff --git a/a.py b/a.py\n"
    key_file = tmp_path / "api-key"
    if key is not None:
        key_file.write_text(key)
    return ClaudeReviewProvider(
        docker_client=raw,
        worktrees=worktrees,
        projects_root=tmp_path / "projects",
        api_key_file=key_file,
        model="claude-model",
        max_budget_usd=3.0,
    )


def _worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    return worktree


def _exec_result(payload: dict | None, *, exit_code: int = 0, stderr: bytes = b"") -> MagicMock:
    stdout = json.dumps(payload).encode() if payload is not None else b""
    return MagicMock(exit_code=exit_code, output=(stdout, stderr))


def _success(text: str) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, "result": text}


def test_key_missing_means_unavailable_not_broken(tmp_path: Path) -> None:
    provider = _provider(tmp_path, key=None)

    with pytest.raises(ProviderUnavailableError, match="API key"):
        provider.start_review_session(job(), _worktree(tmp_path), profile())

    provider._docker.containers.run.assert_not_called()


def test_empty_key_file_means_unavailable(tmp_path: Path) -> None:
    provider = _provider(tmp_path, key="  \n")

    with pytest.raises(ProviderUnavailableError, match="API key"):
        provider.start_review_session(job(), _worktree(tmp_path), profile())


def test_image_missing_means_unavailable(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    provider._docker.images.get.side_effect = ImageNotFound("no such image")

    with pytest.raises(ProviderUnavailableError, match="image"):
        provider.start_review_session(job(), _worktree(tmp_path), profile())


def test_container_is_confined_to_a_read_only_checkout(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    worktree = _worktree(tmp_path)

    session = provider.start_review_session(job(), worktree, profile())

    kwargs = provider._docker.containers.run.call_args.kwargs
    assert session.container == "claude-review-3"
    assert kwargs["volumes"] == {str(worktree): {"bind": "/workspace", "mode": "ro"}}
    assert kwargs["working_dir"] == "/workspace"
    assert kwargs["user"] == f"{claude_mod.AGENT_UID}:{claude_mod.AGENT_GID}"
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges"]
    # Unlike a codex session it needs no MCP gateway and no github token, so it
    # stays off the operator's service network altogether.
    assert kwargs["network"] == "bridge"
    assert "ROUTER_INTERNAL_URL" not in kwargs["environment"]
    assert kwargs["environment"]["ANTHROPIC_API_KEY"] == "sk-test-key"


def test_key_never_reaches_argv(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    container = provider._docker.containers.get.return_value
    container.exec_run.return_value = _exec_result(_success("VERDICT: APPROVE"))
    provider.start_review_session(job(), _worktree(tmp_path), profile())

    provider.run_review(SESSION, "prompt", 60)

    assert "sk-test-key" not in " ".join(container.exec_run.call_args.args[0])


def test_the_pr_diff_is_written_where_the_prompt_says(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    worktree = _worktree(tmp_path)
    container = provider._docker.containers.get.return_value
    container.exec_run.return_value = _exec_result(_success("VERDICT: APPROVE"))

    provider.start_review_session(job(), worktree, profile())
    provider.run_review(SESSION, "the prompt", 60)

    assert (worktree / claude_mod.DIFF_FILENAME).read_text() == "diff --git a/a.py b/a.py\n"
    provider._worktrees.pr_diff.assert_called_once_with(
        repo_dir=tmp_path / "projects" / "homelab" / "repo", base_sha="base", head_sha="head"
    )
    assert f"/workspace/{claude_mod.DIFF_FILENAME}" in claude_mod.review_prompt("the prompt")
    assert claude_mod.review_prompt("the prompt").endswith("the prompt")


def test_run_review_is_bare_read_only_and_bounded(tmp_path: Path) -> None:
    # --bare is what keeps the checkout's own .claude/settings.json hooks and
    # .mcp.json from running: the checkout is the code under review.
    provider = _provider(tmp_path)
    container = provider._docker.containers.get.return_value
    container.exec_run.return_value = _exec_result(_success("VERDICT: APPROVE"))

    provider.run_review(SESSION, "x" * 300_000, 60)

    cmd = container.exec_run.call_args.args[0]
    assert all(len(part) < 1_000 for part in cmd)
    command = cmd[-1]
    assert command.startswith("claude --bare -p ")
    assert "--tools Read,Glob,Grep " in command
    assert "--permission-mode dontAsk " in command
    assert "--model claude-model " in command
    assert "--max-budget-usd 3.0" in command
    assert command.endswith(f"< {claude_mod.PROMPT_PATH}")
    assert container.exec_run.call_args.kwargs["demux"] is True


@pytest.mark.parametrize(
    ("text", "event"),
    [
        ("No findings.\n\nVERDICT: APPROVE", "APPROVE"),
        ("`a.py:1` is wrong.\n\nVERDICT: REQUEST_CHANGES", "REQUEST_CHANGES"),
        ("No verdict line at all", "REQUEST_CHANGES"),
    ],
)
def test_run_review_returns_the_parsed_verdict(tmp_path: Path, text: str, event: str) -> None:
    provider = _provider(tmp_path)
    container = provider._docker.containers.get.return_value
    container.exec_run.return_value = _exec_result(_success(text))

    result = provider.run_review(SESSION, "prompt", 60)

    assert result.event == event
    assert "VERDICT" not in result.body


# Both payloads below are what claude 2.1 actually printed for these failures:
# `subtype` says "success" with `is_error` true, so subtype cannot be trusted.
@pytest.mark.parametrize(
    "payload",
    [
        {
            "subtype": "success",
            "is_error": True,
            "api_error_status": 401,
            "terminal_reason": "api_error",
            "result": "Invalid API key · Fix external API key",
        },
        {
            "subtype": "success",
            "is_error": True,
            "api_error_status": None,
            "terminal_reason": "api_error",
            "result": "Failed to authenticate: OAuth session expired",
        },
        {"subtype": "success", "is_error": True, "api_error_status": 429, "result": "Too many"},
        {"subtype": "success", "is_error": True, "result": "Credit balance is too low"},
        {"subtype": "success", "is_error": True, "result": "You've hit your monthly spend limit"},
    ],
)
def test_auth_and_credit_failures_are_unavailable(tmp_path: Path, payload: dict) -> None:
    provider = _provider(tmp_path)
    container = provider._docker.containers.get.return_value
    container.exec_run.return_value = _exec_result(payload, exit_code=1)

    with pytest.raises(ProviderUnavailableError, match=str(payload["result"])):
        provider.run_review(SESSION, "prompt", 60)


@pytest.mark.parametrize(
    "payload",
    [
        {"subtype": "error_max_turns", "is_error": True, "errors": ["ran out of turns"]},
        {"subtype": "error_during_execution", "is_error": False, "errors": ["it broke"]},
        {"subtype": "success", "is_error": False, "result": "   "},
    ],
)
def test_other_failures_are_plain_errors_for_the_worker_to_retry(
    tmp_path: Path, payload: dict
) -> None:
    provider = _provider(tmp_path)
    container = provider._docker.containers.get.return_value
    container.exec_run.return_value = _exec_result(payload)

    with pytest.raises(RuntimeError) as exc:
        provider.run_review(SESSION, "prompt", 60)

    assert not isinstance(exc.value, ProviderUnavailableError)


def test_output_that_is_not_json_surfaces_stderr(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    container = provider._docker.containers.get.return_value
    container.exec_run.return_value = MagicMock(
        exit_code=1, output=(b"", b"DISTINCTIVE_MARKER_42\n")
    )

    with pytest.raises(RuntimeError, match="DISTINCTIVE_MARKER_42"):
        provider.run_review(SESSION, "prompt", 60)


def test_cleanup_removes_the_container(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    container = provider._docker.containers.get.return_value

    provider.cleanup(SESSION)

    container.remove.assert_called_once_with(force=True)


def test_a_start_failure_leaves_no_container_behind(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    provider._docker.containers.run.side_effect = RuntimeError("start timed out")
    container = provider._docker.containers.get.return_value

    with pytest.raises(RuntimeError, match="start timed out"):
        provider.start_review_session(job(), _worktree(tmp_path), profile())

    assert container.remove.call_count == 2  # the stale name first, then the failed start
