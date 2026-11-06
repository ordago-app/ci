from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from src.main import build_claude_providers
from src.provider import ProviderUnavailable

from .test_provider_claude import _docker, job
from .test_provider_codex import profile


def _environment_of(account: str, env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw = _docker(f"claude-review-3-{account}")
    _build(env, raw, tmp_path)[account].start_review_session(job(), worktree, profile())
    return raw.containers.run.call_args.kwargs["environment"]


def _build(env: dict[str, str], raw: MagicMock, tmp_path: Path):
    return build_claude_providers(
        env,
        docker_client=raw,
        projects_root=tmp_path / "projects",
        router_url="http://claude-router:8000",
        model="claude-opus-5",
    )


ALL_CREDENTIALS = {
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-work",
    "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY": "sk-ant-oat01-personal",
    "CLAUDE_REVIEW_API_KEY": "sk-ant-api03-review",
}


def test_claude_api_runs_on_the_review_key_alone(tmp_path: Path) -> None:
    environment = _environment_of("claude-api", ALL_CREDENTIALS, tmp_path)

    assert environment["ANTHROPIC_API_KEY"] == "sk-ant-api03-review"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in environment


@pytest.mark.parametrize(
    ("account", "token"),
    [("claude-work", "sk-ant-oat01-work"), ("claude-personal", "sk-ant-oat01-personal")],
)
def test_plan_accounts_run_on_their_own_token_alone(
    account: str, token: str, tmp_path: Path
) -> None:
    environment = _environment_of(account, ALL_CREDENTIALS, tmp_path)

    assert environment["CLAUDE_CODE_OAUTH_TOKEN"] == token
    assert "ANTHROPIC_API_KEY" not in environment


@pytest.mark.parametrize("env", [{}, {"CLAUDE_REVIEW_API_KEY": ""}])
def test_an_unset_review_key_stays_registered_and_fails_over(
    env: dict[str, str], tmp_path: Path
) -> None:
    """Dropped from the registry, a chain naming it would stop the service at
    config load instead of moving on to the next account."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    provider = _build(env, _docker(), tmp_path)["claude-api"]

    with pytest.raises(ProviderUnavailable) as caught:
        provider.start_review_session(job(), worktree, profile())
    assert caught.value.provider == "claude-api"
    assert caught.value.reason == "auth"
