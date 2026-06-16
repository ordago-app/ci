from pathlib import Path
from unittest.mock import MagicMock

import pytest
from src.config import ToolProfile
from src.job_store import JobStatus, ReviewJob
from src.providers.codex import CodexReviewProvider


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


def profile() -> ToolProfile:
    return ToolProfile(
        github_role="reviewer",
        mcps={"github": ["ro"]},
        allow_shell=True,
        allow_network=True,
        allow_repo_write=True,
        allow_git_push=False,
        allow_docker=False,
        max_runtime_minutes=30,
    )


def test_starts_codex_review_container(tmp_path: Path) -> None:
    raw = MagicMock()
    image = MagicMock()
    image.id = "sha256:image"
    raw.images.get.return_value = image
    container = MagicMock()
    container.name = "codex-review-3"
    raw.containers.run.return_value = container
    provider = CodexReviewProvider(
        docker_client=raw,
        projects_root=tmp_path / "projects",
        codex_state_root=tmp_path / "codex-state",
        router_url="http://claude-router:8000",
        model="gpt-5.5",
    )
    (tmp_path / "codex-state").mkdir()
    (tmp_path / "codex-state" / "auth.json").write_text("{}")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    session = provider.start_review_session(job(), worktree, profile())

    raw.images.get.assert_called_once_with("homelab/codex-code-homelab:latest")
    assert session.container == "codex-review-3"
    assert (tmp_path / "projects" / "homelab" / "review-sessions" / "3" / "config.toml").exists()
    env = raw.containers.run.call_args.kwargs["environment"]
    assert env["AGENT_ROLE"] == "reviewer"


def test_makes_worktree_and_codex_home_agent_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: github-review runs as root and creates the worktree and
    # CODEX_HOME as root, but the Codex container runs as the non-root `agent`
    # user (uid 1000), which then can't write its own home -> "Permission
    # denied (os error 13)". The provider must chown both to the agent uid.
    import os

    from src.providers import codex as codex_mod

    raw = MagicMock()
    image = MagicMock()
    image.id = "sha256:image"
    raw.images.get.return_value = image
    raw.containers.run.return_value = MagicMock(name="codex-review-3")

    chowned: list[tuple[str, int, int]] = []
    monkeypatch.setattr(os, "chown", lambda p, u, g: chowned.append((str(p), u, g)))
    monkeypatch.setattr(os, "lchown", lambda p, u, g: chowned.append((str(p), u, g)))

    provider = CodexReviewProvider(
        docker_client=raw,
        projects_root=tmp_path / "projects",
        codex_state_root=tmp_path / "codex-state",
        router_url="http://claude-router:8000",
        model="gpt-5.5",
    )
    (tmp_path / "codex-state").mkdir()
    (tmp_path / "codex-state" / "auth.json").write_text("{}")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    provider.start_review_session(job(), worktree, profile())

    codex_home = tmp_path / "projects" / "homelab" / "review-sessions" / "3"
    chowned_paths = {p for p, _, _ in chowned}
    assert str(worktree) in chowned_paths, "worktree must be chowned to the agent uid"
    assert str(codex_home) in chowned_paths, "CODEX_HOME must be chowned to the agent uid"
    assert all((u, g) == (codex_mod.AGENT_UID, codex_mod.AGENT_GID) for _, u, g in chowned)
