import textwrap
from pathlib import Path

import pytest
from src.config import ConfigError, ReviewConfig


def write(path: Path, text: str) -> Path:
    # dedent before strip: the YAML blocks below are uniformly indented, so a
    # bare strip() would leave the first key at column 0 and the rest indented,
    # which is not valid YAML.
    path.write_text(textwrap.dedent(text).strip() + "\n")
    return path


def test_loads_codex_repo_and_profile(tmp_path: Path) -> None:
    cfg_path = write(
        tmp_path / "agent-review.yml",
        """
        repos:
          homelab:
            repo: alvaro-francisco-gil/homelab
            project: homelab
            provider: codex
            enabled: true
            review_drafts: false
            run_ci_first: true
            tool_profile: default-reviewer
        tool_profiles:
          default-reviewer:
            github_role: reviewer
            mcps:
              github: [ro]
            allow_shell: true
            allow_network: true
            allow_repo_write: true
            allow_git_push: false
            allow_docker: false
            max_runtime_minutes: 30
        """,
    )
    cfg = ReviewConfig.load(cfg_path)
    repo = cfg.enabled_repos()[0]
    assert repo.name == "homelab"
    assert repo.repo == "alvaro-francisco-gil/homelab"
    assert repo.provider == "codex"
    assert cfg.profile_for(repo).github_role == "reviewer"


def test_rejects_unknown_provider(tmp_path: Path) -> None:
    cfg_path = write(
        tmp_path / "agent-review.yml",
        """
        repos:
          homelab:
            repo: alvaro-francisco-gil/homelab
            project: homelab
            provider: other
            enabled: true
            review_drafts: false
            run_ci_first: true
            tool_profile: default-reviewer
        tool_profiles:
          default-reviewer:
            github_role: reviewer
            mcps: {}
            allow_shell: true
            allow_network: true
            allow_repo_write: true
            allow_git_push: false
            allow_docker: false
            max_runtime_minutes: 30
        """,
    )
    with pytest.raises(ConfigError, match="unknown provider"):
        ReviewConfig.load(cfg_path)


def test_rejects_missing_profile(tmp_path: Path) -> None:
    cfg_path = write(
        tmp_path / "agent-review.yml",
        """
        repos:
          homelab:
            repo: alvaro-francisco-gil/homelab
            project: homelab
            provider: codex
            enabled: true
            review_drafts: false
            run_ci_first: true
            tool_profile: missing
        tool_profiles: {}
        """,
    )
    with pytest.raises(ConfigError, match="unknown tool_profile"):
        ReviewConfig.load(cfg_path)


def test_rejects_git_push_for_reviewer(tmp_path: Path) -> None:
    cfg_path = write(
        tmp_path / "agent-review.yml",
        """
        repos:
          homelab:
            repo: alvaro-francisco-gil/homelab
            project: homelab
            provider: codex
            enabled: true
            review_drafts: false
            run_ci_first: true
            tool_profile: default-reviewer
        tool_profiles:
          default-reviewer:
            github_role: reviewer
            mcps: {}
            allow_shell: true
            allow_network: true
            allow_repo_write: true
            allow_git_push: true
            allow_docker: false
            max_runtime_minutes: 30
        """,
    )
    with pytest.raises(ConfigError, match="allow_git_push"):
        ReviewConfig.load(cfg_path)
