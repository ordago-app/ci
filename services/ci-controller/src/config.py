from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError, field_validator, model_validator


class ConfigError(ValueError):
    """Raised when the controller config file is missing or invalid."""


class JobClass(BaseModel):
    ram_mb: int
    needs_kvm: bool = False
    needs_android_sdk: bool = False
    work_disk: str = "ssd"
    group_add: list[str] = []

    @field_validator("work_disk")
    @classmethod
    def work_disk_known(cls, value: str) -> str:
        if value not in {"ssd", "hdd"}:
            raise ValueError("work_disk must be 'ssd' or 'hdd'")
        return value


class RepoConfig(BaseModel):
    repo: str
    label_class: dict[str, str] = {}


class Mount(BaseModel):
    host: str
    container: str


class ControllerConfig(BaseModel):
    ram_budget_mb: int
    max_concurrent_lanes: int
    default_class: str
    runner_image: str
    work_dirs: dict[str, str]
    shared_mounts: list[Mount] = []
    classes: dict[str, JobClass]
    repos: list[RepoConfig]

    @model_validator(mode="after")
    def _check_references(self) -> ControllerConfig:
        if self.default_class not in self.classes:
            raise ValueError(f"default_class '{self.default_class}' is not a defined class")
        for disk in ("ssd", "hdd"):
            if disk not in self.work_dirs:
                raise ValueError(f"work_dirs must define '{disk}'")
        for repo in self.repos:
            for label, class_name in repo.label_class.items():
                if class_name not in self.classes:
                    raise ValueError(
                        f"repo {repo.repo}: label '{label}' maps to unknown class '{class_name}'"
                    )
        return self

    @classmethod
    def load(cls, path: Path) -> ControllerConfig:
        try:
            raw = yaml.safe_load(path.read_text())
            return cls.model_validate(raw)
        except (OSError, ValidationError, yaml.YAMLError) as exc:
            raise ConfigError(f"invalid ci-controller config {path}: {exc}") from exc

    def repo_names(self) -> set[str]:
        return {repo.repo for repo in self.repos}

    def class_for(self, repo: str, labels: list[str]) -> str | None:
        match = next((r for r in self.repos if r.repo == repo), None)
        if match is None:
            return None
        for label in labels:
            if label in match.label_class:
                return match.label_class[label]
        # No mapped label: only serve genuine self-hosted jobs. A job without the
        # "self-hosted" label (e.g. ubuntu-latest) runs on GitHub-hosted runners and
        # must never be admitted — the controller can't run it and would spawn an
        # idle lane. A self-hosted job with an unmapped custom label gets default_class.
        if "self-hosted" in labels:
            return self.default_class
        return None
