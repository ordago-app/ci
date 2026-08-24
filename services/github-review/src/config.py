from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator


class ConfigError(ValueError):
    pass


class RepoPolicy(BaseModel):
    # name, project and repo are all filled in by ReviewConfig.load from the
    # config's own map key: the key IS the project slug, and personal/repos.yml
    # is the single place that slug's GitHub `owner/name` is written.
    name: str = Field(default="")
    repo: str = Field(default="")
    project: str = Field(default="")
    provider: str
    enabled: bool = True
    review_drafts: bool = False
    review_bots: bool = False
    run_ci_first: bool = True
    review_mode: str = "all"
    tool_profile: str

    @field_validator("review_mode")
    @classmethod
    def review_mode_is_known(cls, value: str) -> str:
        if value not in {"all", "labeled"}:
            raise ValueError("review_mode must be 'all' or 'labeled'")
        return value


class ToolProfile(BaseModel):
    github_role: str = "reviewer"
    mcps: dict[str, list[str]] = Field(default_factory=dict)
    allow_shell: bool = True
    allow_network: bool = True
    allow_repo_write: bool = True
    allow_git_push: bool = False
    allow_docker: bool = False
    max_runtime_minutes: int = 30

    @field_validator("max_runtime_minutes")
    @classmethod
    def runtime_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_runtime_minutes must be positive")
        return value


def _load_project_repos(path: Path) -> dict[str, str]:
    """personal/repos.yml: `project_repos: {<project>: "<owner>/<name>"}`.

    The one place a project's GitHub repo is written. A malformed or missing
    entry raises rather than being skipped: a project quietly absent from here
    becomes a repo nobody reviews, which looks exactly like a quiet day."""
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"invalid repo registry {path}: {exc}") from exc
    raw = doc.get("project_repos") if isinstance(doc, dict) else None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: missing a `project_repos:` mapping")
    out: dict[str, str] = {}
    for project, repo in raw.items():
        if not isinstance(repo, str) or repo.count("/") != 1 or not all(repo.split("/")):
            raise ConfigError(f"{path}: project {project!r} must map to 'owner/name', got {repo!r}")
        out[str(project)] = repo
    return out


class _RawConfig(BaseModel):
    repos: dict[str, RepoPolicy]
    tool_profiles: dict[str, ToolProfile]


@dataclass(frozen=True)
class ReviewConfig:
    repos: dict[str, RepoPolicy]
    tool_profiles: dict[str, ToolProfile]

    @classmethod
    def load(cls, path: Path, repos_path: Path | None = None) -> ReviewConfig:
        """`repos_path` defaults to repos.yml beside the review config, which is
        how both the container (`/etc/github-review/`) and the operator checkout
        (`personal/`) lay them out."""
        registry_path = repos_path or path.parent / "repos.yml"
        project_repos = _load_project_repos(registry_path)
        try:
            raw_doc = yaml.safe_load(path.read_text()) or {}
            raw = _RawConfig.model_validate(raw_doc)
        except (OSError, ValidationError, yaml.YAMLError) as exc:
            raise ConfigError(f"invalid review config {path}: {exc}") from exc

        repos: dict[str, RepoPolicy] = {}
        for name, repo in raw.repos.items():
            if repo.provider not in ("codex", "claude"):
                raise ConfigError(f"unknown provider for {name}: {repo.provider}")
            if repo.tool_profile not in raw.tool_profiles:
                raise ConfigError(f"unknown tool_profile for {name}: {repo.tool_profile}")
            profile = raw.tool_profiles[repo.tool_profile]
            if profile.allow_git_push:
                raise ConfigError(f"tool_profile {repo.tool_profile} sets allow_git_push=true")
            if name not in project_repos:
                raise ConfigError(
                    f"no GitHub repo declared for project {name!r} in {registry_path}"
                )
            repos[name] = repo.model_copy(
                update={"name": name, "project": name, "repo": project_repos[name]}
            )
        return cls(repos=repos, tool_profiles=dict(raw.tool_profiles))

    def enabled_repos(self) -> list[RepoPolicy]:
        return [repo for repo in self.repos.values() if repo.enabled]

    def profile_for(self, repo: RepoPolicy) -> ToolProfile:
        return self.tool_profiles[repo.tool_profile]
