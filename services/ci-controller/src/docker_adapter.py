from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import docker
import docker.errors

from src.config import ControllerConfig, HostConfig
from src.models import AdmitDecision

LANE_LABEL = "com.homelab.ci-controller.lane"
JOB_LABEL = "com.homelab.ci-controller.job"
# The lane's class, stamped at spawn so a restarted controller can re-adopt the lane at
# the reserve it was actually admitted under. The container labels are the only record
# that survives losing the in-memory ledger.
CLASS_LABEL = "com.homelab.ci-controller.class"
# The lane's host, for the same reason: with more than one host the ledger must know
# WHICH host's budget a re-adopted lane spends.
HOST_LABEL = "com.homelab.ci-controller.host"


def _cpu_percent(stats: dict) -> float:
    cpu = stats["cpu_stats"]
    pre = stats["precpu_stats"]
    cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
    sys_delta = cpu["system_cpu_usage"] - pre["system_cpu_usage"]
    online = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage", [])) or 1
    if sys_delta <= 0 or cpu_delta < 0:
        return 0.0
    return (cpu_delta / sys_delta) * online * 100.0


@dataclass(frozen=True)
class LaneInfo:
    lane_id: str
    job_id: int
    container_id: str
    # None for containers spawned before CLASS_LABEL existed — the caller must price
    # an unknown class conservatively rather than assume the default.
    class_name: str | None = None
    # None for lanes spawned before HOST_LABEL existed — same tolerance as class_name.
    host: str | None = None


class DockerAdapter:
    def __init__(
        self,
        client: docker.DockerClient,
        config: ControllerConfig,
        host: str,
        host_config: HostConfig,
    ) -> None:
        self._client = client
        self._config = config
        self._host = host
        self._host_config = host_config

    def spawn(self, decision: AdmitDecision, registration_token: str) -> str:
        job = decision.job
        lane_id = f"{self._host}-cici-{job.job_id}"
        job_class = self._config.classes[decision.class_name]

        # Bind the work_dir BASE (pre-created by ansible, owned by the runner uid) rather
        # than a per-lane subdir: if we bind a non-existent per-lane path, docker creates
        # it root-owned and the uid-1000 runner can't write its workspace ("Set up job"
        # fails). Binding the 1000-owned base lets the entrypoint's `mkdir -p` create the
        # per-lane subdir as the runner user.
        work_dirs = self._host_config.work_dirs or {}
        work_base = work_dirs[job_class.work_disk]
        work_dir = f"/runner-base/{lane_id}-work"
        volumes: dict[str, dict[str, str]] = {work_base: {"bind": "/runner-base", "mode": "rw"}}
        for mount in self._host_config.shared_mounts or []:
            volumes[mount.host] = {"bind": mount.container, "mode": "rw"}

        # Start from the operator-configured lane_env (cache-path parity with the
        # static runner pool — GRADLE_USER_HOME, PNPM_HOME, ANDROID_*), then layer the
        # per-lane RUNNER_* on top so they always win.
        environment = dict(self._host_config.lane_env or {})
        environment.update(
            {
                "RUNNER_REPOSITORY": job.repo,
                "RUNNER_NAME": lane_id,
                "RUNNER_LABELS": ",".join(job.labels),
                "RUNNER_WORKDIR": work_dir,
                "RUNNER_EPHEMERAL": "1",
                "RUNNER_REGISTRATION_TOKEN": registration_token,
                "SKIP_ANDROID_SDK": "0" if job_class.needs_android_sdk else "1",
            }
        )

        run_kwargs: dict[str, object] = {
            "detach": True,
            "auto_remove": True,
            "init": True,
            "name": f"github-runner-{lane_id}",
            "network": self._host_config.docker_network,
            "environment": environment,
            "volumes": volumes,
            "labels": {
                LANE_LABEL: lane_id,
                JOB_LABEL: str(job.job_id),
                CLASS_LABEL: decision.class_name,
                HOST_LABEL: self._host,
            },
        }
        if decision.needs_kvm:
            run_kwargs["devices"] = ["/dev/kvm:/dev/kvm:rwm"]
        if job_class.group_add:
            run_kwargs["group_add"] = list(job_class.group_add)
        # Relative CPU weight, never a cap — see "Resource policy on a machine the
        # operator is using" in the plan. Omitted entirely when None so powerserver's
        # containers stay byte-identical to before per-host config existed.
        if self._host_config.cpu_shares is not None:
            run_kwargs["cpu_shares"] = self._host_config.cpu_shares

        self._client.containers.run(self._config.runner_image, **run_kwargs)
        return lane_id

    def list_lanes(self) -> list[LaneInfo]:
        containers = self._client.containers.list(filters={"label": LANE_LABEL})
        lanes: list[LaneInfo] = []
        for container in containers:
            labels = container.labels
            lanes.append(
                LaneInfo(
                    lane_id=labels[LANE_LABEL],
                    job_id=int(labels[JOB_LABEL]),
                    container_id=container.id,
                    class_name=labels.get(CLASS_LABEL),
                    host=labels.get(HOST_LABEL),
                )
            )
        return lanes

    def ping(self) -> bool:
        """False on ANY exception. A sleeping desktop, an unreachable tailnet
        address, and a dead daemon are the same event to the controller — a
        raised exception here would kill the reconcile tick."""
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    def remove(self, container_id: str) -> None:
        try:
            container = self._client.containers.get(container_id)
            container.remove(force=True)
        except docker.errors.NotFound:
            pass

    def sample(self, container_id: str) -> tuple[int, float] | None:
        try:
            container = self._client.containers.get(container_id)
            stats = container.stats(stream=False)
            memory = stats["memory_stats"]
            # `usage` counts reclaimable page cache, which for CI lanes dwarfs the
            # anonymous working set: a git checkout plus reads from the shared pnpm
            # store are charged to whichever cgroup first touched those pages. Left
            # uncorrected it recorded >12 GB peaks for jobs that only run `grep`,
            # making every reservation tuned against this metric wrong.
            # Subtracting inactive_file is what `docker stats` reports as MEM USAGE.
            inactive_file = int(memory.get("stats", {}).get("inactive_file", 0))
            ram_mb = max(0, int(memory["usage"]) - inactive_file) // (1024 * 1024)
            return ram_mb, _cpu_percent(stats)
        except (docker.errors.NotFound, KeyError, TypeError):
            return None


class DockerPool:
    """One DockerAdapter per configured host, built from `resolved_hosts()`.

    `client_factory` is injected so tests can construct a pool without real
    docker sockets; it defaults to a real `docker.DockerClient`.
    """

    def __init__(
        self,
        config: ControllerConfig,
        client_factory: Callable[[str], docker.DockerClient] | None = None,
    ) -> None:
        factory = client_factory or (lambda endpoint: docker.DockerClient(base_url=endpoint))
        self._adapters: dict[str, DockerAdapter] = {
            name: DockerAdapter(
                client=factory(host_config.docker_endpoint),
                config=config,
                host=name,
                host_config=host_config,
            )
            for name, host_config in config.resolved_hosts().items()
        }

    def for_host(self, name: str) -> DockerAdapter:
        return self._adapters[name]

    def snapshot(self) -> tuple[set[str], dict[str, list[LaneInfo]]]:
        """One observation per host: which hosts answered, and what they are running.

        Health and lane listing MUST come from the same pass. Asking twice — a
        `healthy()` call, then a separate listing — lets the two answers disagree:
        ping succeeds, the listing then fails, and the caller sees a host it believes
        is healthy with none of its lanes. That is indistinguishable from "every lane
        finished", so the reaper frees live reservations after a single blip,
        bypassing the host_unhealthy_ticks debounce entirely.

        A host therefore counts as healthy only if it BOTH answers ping and lists its
        lanes. Neither call raises: a host that is asleep, unreachable, or has a dead
        daemon is simply absent from both return values.
        """
        healthy: set[str] = set()
        lanes: dict[str, list[LaneInfo]] = {}
        for name, adapter in self._adapters.items():
            if not adapter.ping():
                continue
            try:
                lanes[name] = adapter.list_lanes()
            except Exception:
                continue  # answered, but cannot be trusted to report its lanes
            healthy.add(name)
        return healthy, lanes
