from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


class ConfigError(ValueError):
    """Raised when the controller config file is missing or invalid."""


def _load_project_repos(path: Path) -> dict[str, str]:
    """personal/repos.yml: `project_repos: {<project>: "<owner>/<name>"}`.

    The one place a project's GitHub repo is written. A malformed or missing
    entry raises rather than being skipped: a project quietly absent from here
    is a repo the controller stops provisioning lanes for, which presents as
    jobs that queue forever with nothing in the logs."""
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
    """`project` is the slug; `repo` is its GitHub `owner/name`, filled in by
    ControllerConfig.load from personal/repos.yml. The full name is not written
    here because it is written there, and a repo that moves between accounts
    must not need editing in two files to keep its App token minted from the
    right installation."""

    project: str
    repo: str = ""
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
    # Which GitHub orgs may place work on this host. `None` means no restriction,
    # which is what every host meant before this field existed -- absent config must
    # never silently deny. NOT inherited from a top-level default on purpose: the
    # federated pool's whole point is that hosts differ here, so a fleet-wide default
    # would be a footgun that quietly widens a trust boundary.
    #
    # The SAME fact generates the fabric's tailnet ACL (scripts/gen_tailscale_acl.py),
    # so placement can never grant reach the network refuses. See
    # docs/plans/ideas/federated-ci-pool.md decision 6.
    allowed_orgs: list[str] | None = None
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
    # Per-host so a lane host can run an image built without the Android SDK it is
    # not allowed to schedule anyway (see allowed_classes). The proxy denies IMAGES
    # and BUILD, so whatever is named here must already exist on that host or every
    # admission 404s.
    runner_image: str | None = None
    # Where a lane's workspace lives, and therefore WHO reclaims it. See ADR 0023.
    #
    # "bind" binds work_dirs[class.work_disk] and the lane writes a <lane_id>-work
    # subdir inside it. That is the only mode that can place a workspace on a CHOSEN
    # filesystem, which powerserver needs: work_disk routes ssd -> /mnt/ci-ssd (NVMe)
    # while the docker root is on the 7200rpm LVM, and pnpm only hardlinks out of its
    # store when the store and the workspace share a filesystem. Reclamation is the
    # entrypoint's EXIT trap, backed by the controller deleting the dir on reap.
    #
    # "volume" gives the lane an anonymous docker volume instead, which dockerd
    # deletes with the container. It cannot honour work_disk (every volume lands
    # under the docker root), so it is only correct where placement is meaningless.
    # In exchange it is the one mode whose cleanup survives SIGKILL, an OOM kill and
    # a dockerd crash, none of which run an EXIT trap — and it needs no filesystem
    # access from the controller, which is exactly what a REMOTE host cannot give it.
    # `_reap_work_dir` can only unlink paths on the controller's own box, so on a
    # remote host in bind mode the trap is the sole cleanup and a killed lane leaks
    # its workspace forever. That leak filled powervaro-ci on 2026-08-09.
    work_dir_mode: str = "bind"

    @field_validator("work_dir_mode")
    @classmethod
    def work_dir_mode_known(cls, value: str) -> str:
        if value not in ("bind", "volume"):
            raise ValueError("work_dir_mode must be 'bind' or 'volume'")
        return value

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
                "runner_image": (
                    top.runner_image if self.runner_image is None else self.runner_image
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
                # Deliberately passed through rather than defaulted: None stays None
                # and reads downstream as "unrestricted".
                "allowed_orgs": self.allowed_orgs,
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
    # A lane that boots into an empty queue (its job was cancelled during the 20-30s boot)
    # holds its full reserve until some later job happens to match it. Reaping it trades
    # warm capacity for budget, so it is only worth doing when something is actually waiting.
    idle_grace_seconds: float = Field(default=60.0, gt=0)  # may just be booting below this
    idle_lane_max_seconds: float = Field(default=600.0, gt=0)  # absolute ceiling, no pressure req'd
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
                        f"repo {repo.project}: label '{label}' maps to unknown class '{class_name}'"
                    )
        if self.admission_mode not in {"reservation", "reservation_plus_guard"}:
            raise ValueError(
                f"admission_mode '{self.admission_mode}' must be "
                "'reservation' or 'reservation_plus_guard'"
            )
        if self.idle_grace_seconds > self.idle_lane_max_seconds:
            raise ValueError("idle_grace_seconds must not exceed idle_lane_max_seconds")
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
    def load(cls, path: Path, repos_path: Path | None = None) -> ControllerConfig:
        """`repos_path` defaults to repos.yml beside the controller config, which
        is how both the container (`/etc/ci-controller/`) and the operator
        checkout (`personal/`) lay them out."""
        registry_path = repos_path or path.parent / "repos.yml"
        try:
            raw = yaml.safe_load(path.read_text())
            config = cls.model_validate(raw)
        except (OSError, ValidationError, yaml.YAMLError) as exc:
            raise ConfigError(f"invalid ci-controller config {path}: {exc}") from exc
        project_repos = _load_project_repos(registry_path)
        for repo in config.repos:
            if repo.project not in project_repos:
                raise ConfigError(
                    f"no GitHub repo declared for project {repo.project!r} in {registry_path}"
                )
            repo.repo = project_repos[repo.project]
        return config

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
