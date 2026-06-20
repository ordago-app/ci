from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator


class ConfigError(ValueError):
    pass


class RepoPolicy(BaseModel):
    name: str = Field(default="")
    repo: str
    project: str
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

    @field_validator("repo")
    @classmethod
    def repo_must_be_owner_repo(cls, value: str) -> str:
        if value.count("/") != 1 or not all(value.split("/")):
            raise ValueError("repo must be owner/name")
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


class _RawConfig(BaseModel):
    repos: dict[str, RepoPolicy]
    tool_profiles: dict[str, ToolProfile]


@dataclass(frozen=True)
class ReviewConfig:
    repos: dict[str, RepoPolicy]
    tool_profiles: dict[str, ToolProfile]

    @classmethod
    def load(cls, path: Path) -> ReviewConfig:
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
            repos[name] = repo.model_copy(update={"name": name})
        return cls(repos=repos, tool_profiles=dict(raw.tool_profiles))

    def enabled_repos(self) -> list[RepoPolicy]:
        return [repo for repo in self.repos.values() if repo.enabled]

    def profile_for(self, repo: RepoPolicy) -> ToolProfile:
        return self.tool_profiles[repo.tool_profile]
