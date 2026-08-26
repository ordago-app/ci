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


REVIEW_MODE_UNSET_YAML = """
    repos:
      homelab:
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
        allow_git_push: false
        allow_docker: false
        max_runtime_minutes: 30
    """

REVIEW_MODE_BAD_YAML = """
    repos:
      homelab:
        provider: codex
        enabled: true
        review_drafts: false
        run_ci_first: true
        review_mode: nonsense
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
    """


def test_review_mode_defaults_to_all(tmp_path: Path) -> None:
    cfg_path = write(tmp_path / "agent-review.yml", REVIEW_MODE_UNSET_YAML)
    cfg = ReviewConfig.load(cfg_path)
    assert cfg.repos["homelab"].review_mode == "all"


def test_invalid_review_mode_rejected(tmp_path: Path) -> None:
    cfg_path = write(tmp_path / "agent-review.yml", REVIEW_MODE_BAD_YAML)
    with pytest.raises(ConfigError, match="review_mode"):
        ReviewConfig.load(cfg_path)


def test_rejects_git_push_for_reviewer(tmp_path: Path) -> None:
    cfg_path = write(
        tmp_path / "agent-review.yml",
        """
        repos:
          homelab:
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


def test_repo_is_resolved_from_the_shared_registry(tmp_path: Path) -> None:
    """The review config names a project; the GitHub owner/name comes from
    personal/repos.yml, so an owner move is a one-line edit in one file."""
    cfg_path = write(
        tmp_path / "agent-review.yml",
        """
        repos:
          homelab:
            provider: codex
            tool_profile: default-reviewer
        tool_profiles:
          default-reviewer:
            github_role: reviewer
""",
    )
    cfg = ReviewConfig.load(cfg_path)
    repo = cfg.repos["homelab"]
    assert repo.repo == "alvaro-francisco-gil/homelab"
    assert repo.project == "homelab"


def test_a_project_missing_from_the_registry_is_rejected(tmp_path: Path) -> None:
    """Silently skipping it would be a repo nobody reviews, which looks exactly
    like a quiet day."""
    (tmp_path / "repos.yml").write_text("project_repos:\n  ordago-apps: acme/ordago-apps\n")
    cfg_path = write(
        tmp_path / "agent-review.yml",
        """
        repos:
          homelab:
            provider: codex
            tool_profile: default-reviewer
        tool_profiles:
          default-reviewer:
            github_role: reviewer
""",
    )
    with pytest.raises(ConfigError, match="homelab"):
        ReviewConfig.load(cfg_path)


def test_a_registry_entry_that_is_not_owner_name_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "repos.yml").write_text("project_repos:\n  homelab: homelab\n")
    cfg_path = write(
        tmp_path / "agent-review.yml",
        """
        repos:
          homelab:
            provider: codex
            tool_profile: default-reviewer
        tool_profiles:
          default-reviewer:
            github_role: reviewer
""",
    )
    with pytest.raises(ConfigError, match="owner/name"):
        ReviewConfig.load(cfg_path)


# `test_the_committed_config_and_registry_agree` is NOT here, deliberately. It
# asserted that one operator's `personal/agent-review.yml` and
# `personal/repos.yml` ship consistently. That is a fact about a DEPLOYMENT, not
# about this platform, and those files live in the consumer's repo. It stays
# with the consumer; re-adding it here would only ever fail.
