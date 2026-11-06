"""Accounts are configuration: main builds one provider per declared account."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from src.config import ConfigError
from src.main import build_providers, required_env
from src.provider import ProviderUnavailable
from src.providers.claude import ClaudeReviewProvider
from src.providers.codex import CodexReviewProvider

from tests.helpers import write_config

from .test_provider_claude import _docker, job

IMAGES = {"CLAUDE_REVIEW_IMAGE": "claude:test", "CODEX_REVIEW_IMAGE": "codex:test"}


def _build(tmp_path: Path, environ: dict[str, str], raw: MagicMock | None = None):
    config = write_config(
        tmp_path,
        {"ordago-apps": {"providers": ["codex", "claude-api-jaime", "claude-personal"]}},
        accounts={
            "codex": {"kind": "codex", "credential": {"file": str(tmp_path / "auth.json")}},
            "claude-api-jaime": {
                "kind": "claude",
                "auth": "api_key",
                "credential": {"env": "CLAUDE_API_KEY_JAIME"},
                "model": "claude-test",
                "max_budget_usd": 2,
            },
            "claude-personal": {
                "kind": "claude",
                "auth": "subscription",
                "credential": {"env": "CLAUDE_OAUTH_PERSONAL"},
                "model": "claude-test",
            },
        },
    )
    worktrees = MagicMock()
    worktrees.pr_diff.return_value = "diff"
    return build_providers(
        config,
        environ={**IMAGES, **environ},
        docker_client=raw if raw is not None else _docker(),
        worktrees=worktrees,
        projects_root=tmp_path / "projects",
    )


def test_a_new_account_is_one_config_entry_and_no_code(tmp_path: Path) -> None:
    raw = _docker()
    providers = _build(tmp_path, {"CLAUDE_API_KEY_JAIME": "sk-ant-api03-j"}, raw)
    assert set(providers) == {"codex", "claude-api-jaime", "claude-personal"}
    assert isinstance(providers["codex"], CodexReviewProvider)
    assert isinstance(providers["claude-api-jaime"], ClaudeReviewProvider)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    providers["claude-api-jaime"].start_review_session(job(), worktree, 60)

    run = raw.containers.run.call_args.kwargs
    assert run["environment"] == {"ANTHROPIC_API_KEY": "sk-ant-api03-j"}
    assert run["name"] == "review-test-claude-api-jaime-3"
    assert run["image"] == "sha256:image"
    raw.images.get.assert_called_with("claude:test")


def test_a_declared_account_without_its_credential_stays_and_fails_over(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Dropped from the registry, a chain naming it would stop the service at
    startup instead of moving on to the next account."""
    providers = _build(tmp_path, {})
    worktree = tmp_path / "wt"
    worktree.mkdir()

    with pytest.raises(ProviderUnavailable) as caught:
        providers["claude-personal"].start_review_session(job(), worktree, 60)

    assert (caught.value.provider, caught.value.reason) == ("claude-personal", "auth")
    assert "claude-personal" in caplog.text


@pytest.mark.parametrize("image", ["CLAUDE_REVIEW_IMAGE", "CODEX_REVIEW_IMAGE"])
def test_the_review_images_have_no_default(tmp_path: Path, image: str) -> None:
    config = write_config(tmp_path)
    environ = {k: v for k, v in IMAGES.items() if k != image}

    with pytest.raises(ConfigError, match=image):
        build_providers(
            config,
            environ=environ,
            docker_client=MagicMock(),
            worktrees=MagicMock(),
            projects_root=tmp_path,
        )


@pytest.mark.parametrize("value", [None, "", "  "])
def test_required_env_fails_loudly(value: str | None) -> None:
    environ = {} if value is None else {"PROJECTS_ROOT": value}
    with pytest.raises(ConfigError, match="PROJECTS_ROOT"):
        required_env(environ, "PROJECTS_ROOT")
