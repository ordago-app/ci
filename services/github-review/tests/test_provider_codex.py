import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from docker.errors import ImageNotFound
from src.config import CodexAccount, CredentialSource
from src.job_store import JobStatus, ReviewJob
from src.provider import ProviderUnavailable, SessionRef
from src.providers import _agent_container as agent_container
from src.providers._agent_container import PROMPT_PATH
from src.providers.codex import CODEX_HOME, CodexReviewProvider, render_codex_config

from tests.helpers import login

TIMEOUT = 30 * 60
IMAGE = "ordago-ci/codex-review:test"


def _provider(
    raw: MagicMock | None = None,
    projects_root: Path = Path("/x"),
    login_file: Path = Path("/y/auth.json"),
    account: str = "codex",
) -> CodexReviewProvider:
    return CodexReviewProvider(
        account=account,
        settings=CodexAccount(kind="codex", credential=CredentialSource(file=str(login_file))),
        docker_client=raw if raw is not None else MagicMock(),
        projects_root=projects_root,
        image=IMAGE,
        instance="test",
    )


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


def _session_provider(tmp_path: Path, raw: MagicMock) -> CodexReviewProvider:
    raw.images.get.return_value = MagicMock(id="sha256:image")
    state = tmp_path / "codex-state"
    state.mkdir()
    (state / "auth.json").write_text(login())
    (tmp_path / "worktree").mkdir()
    return _provider(raw, tmp_path / "projects", state / "auth.json")


def _session_dir(tmp_path: Path) -> Path:
    return tmp_path / "projects" / "homelab" / "review-sessions" / "3" / "codex"


def _session_copy(tmp_path: Path) -> Path:
    return _session_dir(tmp_path) / "auth.json"


def test_starts_codex_review_container(tmp_path: Path) -> None:
    raw = MagicMock()
    provider = _session_provider(tmp_path, raw)

    session = provider.start_review_session(job(), tmp_path / "worktree", TIMEOUT)

    raw.images.get.assert_called_once_with(IMAGE)
    assert session.container == "review-test-codex-3"
    assert (_session_dir(tmp_path) / "home" / "config.toml").exists()
    run = raw.containers.run.call_args.kwargs
    assert run["environment"] == {"CODEX_HOME": CODEX_HOME}
    # The worktree's `.git` names the clone by host path: mounted there, read-only.
    git_dir = str(tmp_path / "projects" / "homelab" / "repo" / ".git")
    assert run["volumes"][git_dir] == {"bind": git_dir, "mode": "ro"}


def test_the_codex_container_is_sandboxed_like_every_review_container(tmp_path: Path) -> None:
    """Codex runs a shell over the PR's code with a login in reach: it gets the
    same sandbox as Claude, and nothing that reaches GitHub or this service."""
    raw = MagicMock()
    provider = _session_provider(tmp_path, raw)

    provider.start_review_session(job(), tmp_path / "worktree", TIMEOUT)

    run = raw.containers.run.call_args.kwargs
    assert run["network"] == "bridge"
    assert run["user"] == "1000:1000"
    assert run["cap_drop"] == ["ALL"]
    assert run["security_opt"] == ["no-new-privileges"]
    assert run["labels"] == {agent_container.INSTANCE_LABEL: "test"}
    assert run["volumes"][str(tmp_path / "worktree")] == {"bind": "/workspace", "mode": "ro"}
    assert not any("GITHUB" in key or "ROUTER" in key for key in run["environment"])


def test_an_unbuilt_image_is_an_outage_not_a_crash(tmp_path: Path) -> None:
    raw = MagicMock()
    provider = _session_provider(tmp_path, raw)
    raw.images.get.side_effect = ImageNotFound("no such image")

    with pytest.raises(ProviderUnavailable) as caught:
        provider.start_review_session(job(), tmp_path / "worktree", TIMEOUT)

    assert (caught.value.provider, caught.value.reason) == ("codex", "tooling")
    raw.containers.run.assert_not_called()


def test_a_session_reads_a_refreshless_copy_of_the_login_and_cannot_write_it(
    tmp_path: Path,
) -> None:
    # Concurrent sessions sharing the rw auth.json could each refresh its
    # single-use refresh token; the loser logs the account out. A session gets a
    # copy without the refresh token, mounted read-only, and never the original.
    raw = MagicMock()
    provider = _session_provider(tmp_path, raw)
    state = tmp_path / "codex-state"

    provider.start_review_session(job(), tmp_path / "worktree", TIMEOUT)

    volumes = raw.containers.run.call_args.kwargs["volumes"]
    copy = _session_copy(tmp_path)
    assert volumes[str(copy)] == {"bind": f"{CODEX_HOME}/auth.json", "mode": "ro"}
    assert not any(str(state) in host for host in volumes)
    assert not copy.is_symlink()
    assert json.loads(copy.read_text())["tokens"]["refresh_token"] == ""
    assert json.loads((state / "auth.json").read_text())["tokens"]["refresh_token"] == "rt-1"


def test_two_codex_accounts_never_share_a_session(tmp_path: Path) -> None:
    raw = MagicMock()
    _session_provider(tmp_path, raw)
    other_login = tmp_path / "other" / "auth.json"
    other_login.parent.mkdir()
    other_login.write_text(login(refresh_token="rt-other"))
    other = _provider(raw, tmp_path / "projects", other_login, account="codex-spare")

    session = other.start_review_session(job(), tmp_path / "worktree", TIMEOUT)

    assert session.container == "review-test-codex-spare-3"
    spare = tmp_path / "projects" / "homelab" / "review-sessions" / "3" / "codex-spare"
    assert (spare / "auth.json").exists()


def test_the_login_copy_outlives_its_session_by_nothing(tmp_path: Path) -> None:
    # It holds a live access token; review-sessions/ is never pruned otherwise.
    raw = MagicMock()
    provider = _session_provider(tmp_path, raw)
    session = provider.start_review_session(job(), tmp_path / "worktree", TIMEOUT)
    assert _session_copy(tmp_path).stat().st_mode & 0o777 == 0o400

    provider.cleanup(session)

    assert not _session_dir(tmp_path).exists()


def test_a_session_that_never_started_leaves_no_login_copy(tmp_path: Path) -> None:
    raw = MagicMock()
    raw.containers.run.side_effect = RuntimeError("Conflict: name in use")
    provider = _session_provider(tmp_path, raw)

    with pytest.raises(RuntimeError, match="Conflict"):
        provider.start_review_session(job(), tmp_path / "worktree", TIMEOUT)

    assert not _session_copy(tmp_path).exists()


def test_discarding_an_interrupted_session_removes_its_login_copy(tmp_path: Path) -> None:
    raw = MagicMock()
    provider = _session_provider(tmp_path, raw)
    provider.start_review_session(job(), tmp_path / "worktree", TIMEOUT)

    _provider(raw, tmp_path / "projects", tmp_path / "codex-state" / "auth.json").discard(job())

    assert not _session_copy(tmp_path).exists()
    raw.containers.get.return_value.remove.assert_called_with(force=True)


def test_the_login_must_outlast_the_whole_review_plus_the_cli_s_refresh_window(
    tmp_path: Path,
) -> None:
    raw = MagicMock()
    provider = _session_provider(tmp_path, raw)
    asked: list[float] = []
    real = provider._auth.write_session_copy
    provider._auth.write_session_copy = lambda dest, valid_for_seconds: (  # type: ignore[method-assign]
        asked.append(valid_for_seconds),
        real(dest, valid_for_seconds),
    )[-1]

    provider.start_review_session(job(), tmp_path / "worktree", TIMEOUT)

    assert asked == [30 * 60 + 10 * 60]


def test_makes_codex_home_and_login_copy_agent_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: github-review runs as root and creates CODEX_HOME as root, but
    # the codex container runs as uid 1000, which then can't write its own home
    # -> "Permission denied (os error 13)". The worktree is mounted read-only
    # and is not handed over.
    raw = MagicMock()
    provider = _session_provider(tmp_path, raw)
    chowned: list[tuple[str, int, int]] = []
    monkeypatch.setattr(os, "chown", lambda p, u, g: chowned.append((str(p), u, g)))
    monkeypatch.setattr(os, "lchown", lambda p, u, g: chowned.append((str(p), u, g)))

    provider.start_review_session(job(), tmp_path / "worktree", TIMEOUT)

    chowned_paths = {p for p, _, _ in chowned}
    assert str(_session_dir(tmp_path) / "home") in chowned_paths
    assert str(_session_copy(tmp_path)) in chowned_paths
    assert str(tmp_path / "worktree") not in chowned_paths
    expected = (agent_container.AGENT_UID, agent_container.AGENT_GID)
    assert all((u, g) == expected for _, u, g in chowned)


def test_extract_review_body_parses_codex_agent_message() -> None:
    # Regression: codex `exec --json` emits
    # {"type":"item.completed","item":{"type":"agent_message","text":...}},
    # not {"type":"message","content":...}. The old parser matched nothing and
    # raised "codex produced no review body" even on a successful run.
    output = "\n".join(
        [
            '{"type":"item.started","item":{"type":"reasoning"}}',
            '{"type":"item.completed","item":{"type":"command_execution","name":"rg"}}',
            '{"type":"item.completed","item":'
            '{"type":"agent_message","text":"## Review\\nLooks good."}}',
            '{"type":"turn.completed"}',
        ]
    )
    body = _provider()._extract_review_body(output)
    assert body.startswith("## Review")
    assert "Looks good." in body


def test_extract_review_body_surfaces_raw_output_when_empty() -> None:
    # When codex returns no agent_message the error must include the raw output
    # so we can see what it actually emitted, instead of an opaque message.
    output = '{"type":"turn.failed","error":{"message":"DISTINCTIVE_MARKER_42"}}'
    with pytest.raises(RuntimeError) as exc:
        _provider()._extract_review_body(output)
    assert "DISTINCTIVE_MARKER_42" in str(exc.value)


def test_cleanup_removes_container() -> None:
    raw = MagicMock()
    container = MagicMock()
    raw.containers.get.return_value = container
    _provider(raw).cleanup(SessionRef(id="1", container="review-test-codex-1", worktree=Path("/w")))
    container.remove.assert_called_once_with(force=True)


def test_cleanup_surfaces_removal_failure(capsys: pytest.CaptureFixture[str]) -> None:
    # Regression: cleanup() swallowed every exception, so a failing remove()
    # silently leaked one codex container per review. A removal failure must be
    # surfaced, not hidden.
    raw = MagicMock()
    container = MagicMock()
    container.remove.side_effect = RuntimeError("docker daemon boom")
    raw.containers.get.return_value = container
    # Must not raise (runs in a finally), but must log the failure.
    _provider(raw).cleanup(SessionRef(id="1", container="review-test-codex-1", worktree=Path("/w")))
    err = capsys.readouterr().err
    assert "review-test-codex-1" in err
    assert "docker daemon boom" in err


# ── run_review: the branch that decides outage vs finding ─────────────────
# Previously untested end-to-end, which is why the classification leak below
# shipped. These drive the real method, not a fake provider.

_QUOTA = (
    "You've hit your usage limit. Upgrade to Pro "
    "(https://chatgpt.com/explore/pro) ... or try again at Sep 7th, 2026 3:31 AM."
)


def _run(output: str, exit_code: int = 0, prompt: str = "p"):
    raw = MagicMock()
    container = MagicMock()
    container.exec_run.return_value = MagicMock(exit_code=exit_code, output=output.encode())
    raw.containers.get.return_value = container
    return _provider(raw).run_review(
        SessionRef(id="1", container="c", worktree=Path("/w")), prompt, 60
    )


def _msg(text: str) -> str:
    return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})


def test_exit_zero_turn_failed_with_a_quota_message_is_an_outage() -> None:
    out = "\n".join(
        [
            _msg("partial thoughts"),
            json.dumps({"type": "turn.failed", "error": {"message": _QUOTA}}),
        ]
    )

    with pytest.raises(ProviderUnavailable) as caught:
        _run(out, exit_code=0)

    assert caught.value.reason == "exhausted"


def test_exit_zero_turn_failed_with_an_unknown_message_never_posts() -> None:
    """The failed turn is the fact; recognising its wording is a bonus.

    Falling through to publish the trailing agent_message puts a finding on the
    PR that nobody wrote — the phantom-verdict failure of 2026-08-09.
    """
    out = "\n".join(
        [
            _msg("Looks risky.\n\nVERDICT: REQUEST_CHANGES"),
            json.dumps({"type": "turn.failed", "error": {"message": "stream error: EOF"}}),
        ]
    )

    with pytest.raises(RuntimeError) as caught:
        _run(out, exit_code=0)

    assert not isinstance(caught.value, ProviderUnavailable)


def test_a_diff_that_quotes_a_quota_message_is_not_an_outage() -> None:
    """The classifier must never read the transcript.

    A PR *about* rate limiting quotes every signature in the table — this
    repo's own failover PR does. Classifying the body turns any ordinary
    non-zero exit on such a PR into a fabricated outage that spends the
    fallback subscription and slanders codex on the PR.
    """
    out = _msg(f"The diff adds a handler for: {_QUOTA}\n\nVERDICT: APPROVE")

    with pytest.raises(RuntimeError) as caught:
        _run(out, exit_code=1)

    assert not isinstance(caught.value, ProviderUnavailable), (
        "quota wording inside the review body must not be read as an outage"
    )


def test_a_crashed_cli_that_emitted_no_events_is_still_classified() -> None:
    """No JSONL at all means the raw output is pure diagnostics — the one case
    where classifying it is safe, and the case that took the lab down on
    2026-09-01 (`unknown variant \\`max\\``)."""
    with pytest.raises(ProviderUnavailable) as caught:
        _run(
            "failed to refresh available models: unknown variant `max`, "
            "expected one of none|minimal|low|medium|high|xhigh\n",
            exit_code=1,
        )

    assert caught.value.reason == "tooling"


def test_a_clean_run_returns_the_review() -> None:
    result = _run(_msg("All good.\n\nVERDICT: APPROVE"))

    assert result.event == "APPROVE"
    assert result.body == "All good."


def test_an_outage_on_stderr_after_the_stream_started_still_fails_over() -> None:
    """The narrow fix must not swap one bug for another.

    Classifying only structured events would close the body leak but drop a
    real outage that arrives as plain stderr once the CLI has already emitted
    an event — and failing to fail over is the failure this whole chain exists
    to prevent.
    """
    out = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            "codex: You've hit your usage limit. try again at Sep 7th, 2026 3:31 AM.",
        ]
    )

    with pytest.raises(ProviderUnavailable) as caught:
        _run(out, exit_code=1)

    assert caught.value.reason == "exhausted"


def test_the_prompt_echoed_back_is_not_read_as_an_outage() -> None:
    """The prompt embeds the PR title and body, and the CLI echoes its argv.

    A PR *about* rate limiting would otherwise classify itself as an outage —
    the same leak, arriving through the one channel that is legitimately
    non-JSON.
    """
    prompt = "Review this PR: fix(reviewer): handle 'You've hit your usage limit' properly"
    out = "\n".join([prompt, json.dumps({"type": "thread.started", "thread_id": "t"})])

    with pytest.raises(RuntimeError) as caught:
        _run(out, exit_code=1, prompt=prompt)

    assert not isinstance(caught.value, ProviderUnavailable)


def test_a_diagnostic_on_stderr_does_not_discard_a_successful_review() -> None:
    """The most likely real input, and the one the success path never had.

    exec_run folds stderr into the same blob, so ordinary runs carry non-JSON
    tracing. Classifying it throws away a completed review, marks codex down on
    /status, spends the fallback subscription re-reviewing the same PR, and
    tells the PR that codex "could not run" — while it plainly did.
    """
    out = "\n".join(
        [
            "ERROR mcp_client: failed to start server: executable file not found",
            _msg("All good.\n\nVERDICT: APPROVE"),
        ]
    )

    result = _run(out, exit_code=0)

    assert result.event == "APPROVE"
    assert result.body == "All good."


def test_a_quota_line_on_stderr_of_a_successful_run_is_still_not_an_outage() -> None:
    """Even the most on-the-nose wording must not override a completed review.

    A retried-then-succeeded rate limit is a normal thing for a CLI to log.
    """
    out = "\n".join(
        [
            "WARN retrying after 429 rate limit",
            _msg("Fine by me.\n\nVERDICT: APPROVE"),
        ]
    )

    assert _run(out, exit_code=0).event == "APPROVE"


def test_render_codex_config_omits_model_when_unpinned() -> None:
    # Empty model = no `model =` key, so the codex CLI picks whatever the
    # subscription currently serves.
    assert render_codex_config(model="") == ""
    assert render_codex_config(model="gpt-5.5") == 'model = "gpt-5.5"\n'


def _archived_prompt(container: MagicMock) -> str:
    import io
    import tarfile

    target_dir, archive = container.put_archive.call_args.args
    assert target_dir == str(Path(PROMPT_PATH).parent)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        member = tar.getmember(Path(PROMPT_PATH).name)
        assert member.mode == 0o644
        extracted = tar.extractfile(member)
        assert extracted is not None
        return extracted.read().decode("utf-8")


def test_a_large_prompt_reaches_codex_on_stdin_not_argv() -> None:
    """Regression: the prompt embeds the whole diff, and as one argv element it
    failed every PR past the kernel's 128 KiB single-argument cap with
    "argument list too long" (ordago-apps #1628 at 126 KB, #1629 at 280 KB)."""
    raw = MagicMock()
    container = MagicMock()
    container.exec_run.return_value = MagicMock(
        exit_code=0, output=_msg("VERDICT: APPROVE").encode()
    )
    raw.containers.get.return_value = container
    provider = _provider(raw)
    prompt = "x" * 300_000

    provider.run_review(SessionRef(id="1", container="c", worktree=Path("/w")), prompt, 60)

    cmd = container.exec_run.call_args.args[0]
    assert all(len(arg) < 1_000 for arg in cmd)
    assert cmd[:3] == ["sh", "-c", f'exec "$@" < {PROMPT_PATH}']
    assert cmd[4:] == ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "-"]
    assert _archived_prompt(container) == prompt
