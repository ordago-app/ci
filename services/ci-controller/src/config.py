from __future__ import annotations

import hashlib
import json
import os
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
    work_gb: int = 0  # peak per-lane work-dir size; 0 = untracked (no disk gate)
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


class HostConfig(BaseModel):
    """A single host's configuration. Inheritable fields are `None` when the
    per-host key was omitted; `_resolve()` fills them in from the top-level
    config to produce a fully-populated copy."""

    name: str
    docker_endpoint: str
    docker_network: str = "homelab"
    allowed_classes: list[str] | None = None
    cpu_shares: int | None = None
    enabled: bool = True
    ram_budget_mb: int | None = None
    max_concurrent_lanes: int | None = None
    disk_budget_gb: dict[str, int] | None = None
    host_free_ram_floor_mb: int | None = None
    host_load_ceiling: float | None = None
    work_dirs: dict[str, str] | None = None
    shared_mounts: list[Mount] | None = None
    lane_env: dict[str, str] | None = None

    def _resolve(self, top: ControllerConfig) -> HostConfig:
        return self.model_copy(
            update={
                "ram_budget_mb": (
                    top.ram_budget_mb if self.ram_budget_mb is None else self.ram_budget_mb
                ),
                "max_concurrent_lanes": (
                    top.max_concurrent_lanes
                    if self.max_concurrent_lanes is None
                    else self.max_concurrent_lanes
                ),
                "disk_budget_gb": (
                    top.disk_budget_gb if self.disk_budget_gb is None else self.disk_budget_gb
                ),
                "host_free_ram_floor_mb": (
                    top.host_free_ram_floor_mb
                    if self.host_free_ram_floor_mb is None
                    else self.host_free_ram_floor_mb
                ),
                "host_load_ceiling": (
                    top.host_load_ceiling
                    if self.host_load_ceiling is None
                    else self.host_load_ceiling
                ),
                "work_dirs": top.work_dirs if self.work_dirs is None else self.work_dirs,
                "shared_mounts": (
                    top.shared_mounts if self.shared_mounts is None else self.shared_mounts
                ),
                "lane_env": top.lane_env if self.lane_env is None else self.lane_env,
                "allowed_classes": (
                    sorted(top.classes) if self.allowed_classes is None else self.allowed_classes
                ),
            }
        )


class ControllerConfig(BaseModel):
    ram_budget_mb: int
    admission_mode: str = "reservation"
    host_free_ram_floor_mb: int = 1500
    host_load_ceiling: float = 0.0  # 0 disables the load gate
    # Consecutive failed health checks before a lane host is declared lost and its
    # lanes reaped as infra failures. 1 would make a single socket-proxy blip during
    # an ordinary redeploy free every live lane's budget and permanently record green
    # jobs as infra failures. At the 5s poll interval, 3 tolerates ~15s of blindness.
    host_unhealthy_ticks: int = 3
    max_concurrent_lanes: int
    default_class: str
    default_host: str = "powerserver"
    runner_image: str
    work_dirs: dict[str, str]
    disk_budget_gb: dict[str, int] = {}
    shared_mounts: list[Mount] = []
    lane_env: dict[str, str] = {}
    classes: dict[str, JobClass]
    repos: list[RepoConfig]
    hosts: dict[str, HostConfig] = {}

    @model_validator(mode="before")
    @classmethod
    def _fill_host_names(cls, data: object) -> object:
        # HostConfig.name is required but the YAML shape keys hosts by name
        # rather than repeating it inline; fill it in from the map key.
        if isinstance(data, dict) and isinstance(data.get("hosts"), dict):
            for host_name, host_value in data["hosts"].items():
                if isinstance(host_value, dict) and "name" not in host_value:
                    host_value["name"] = host_name
        return data

    @model_validator(mode="after")
    def _check_references(self) -> ControllerConfig:
        if self.default_class not in self.classes:
            raise ValueError(f"default_class '{self.default_class}' is not a defined class")
        for disk in ("ssd", "hdd"):
            if disk not in self.work_dirs:
                raise ValueError(f"work_dirs must define '{disk}'")
        for disk in self.disk_budget_gb:
            if disk not in self.work_dirs:
                raise ValueError(
                    f"disk_budget_gb references unknown disk '{disk}' (not in work_dirs)"
                )
        for repo in self.repos:
            for label, class_name in repo.label_class.items():
                if class_name not in self.classes:
                    raise ValueError(
                        f"repo {repo.repo}: label '{label}' maps to unknown class '{class_name}'"
                    )
        if self.admission_mode not in {"reservation", "reservation_plus_guard"}:
            raise ValueError(
                f"admission_mode '{self.admission_mode}' must be "
                "'reservation' or 'reservation_plus_guard'"
            )
        for host in self.hosts.values():
            if host.allowed_classes is not None:
                for class_name in host.allowed_classes:
                    if class_name not in self.classes:
                        raise ValueError(
                            f"host {host.name}: allowed_classes references "
                            f"unknown class '{class_name}'"
                        )
        if self.hosts and self.default_host not in self.hosts:
            raise ValueError(f"default_host '{self.default_host}' is not in hosts")
        return self

    def resolved_hosts(self) -> dict[str, HostConfig]:
        """Every host, fully populated. When `hosts:` is absent, synthesise a
        single host named by `default_host` from the top-level config, using
        the DOCKER_PROXY_URL default as its docker_endpoint — this is the
        equivalence guarantee for configs written before per-host config
        existed."""
        if not self.hosts:
            default = HostConfig(
                name=self.default_host,
                docker_endpoint=os.environ.get(
                    "DOCKER_PROXY_URL", "tcp://docker-socket-proxy:2375"
                ),
            )
            return {self.default_host: default._resolve(self)}
        return {name: host._resolve(self) for name, host in self.hosts.items()}

    @classmethod
    def load(cls, path: Path) -> ControllerConfig:
        try:
            raw = yaml.safe_load(path.read_text())
            return cls.model_validate(raw)
        except (OSError, ValidationError, yaml.YAMLError) as exc:
            raise ConfigError(f"invalid ci-controller config {path}: {exc}") from exc

    def config_version(self) -> str:
        """8-char hash of the full config; any meaningful change yields a new id."""
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:8]

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
