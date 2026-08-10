from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

import docker
import docker.errors
from docker.errors import DockerException
from docker.types import Mount as DockerMount

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


def _random_suffix() -> str:
    """Six hex chars, drawn per spawn. Lane identity must not be derived from the job id:
    GitHub can hand a lane a different job than the one it was spawned for, which frees
    the spawned-for job for re-admission while the first lane is still running. Two lanes
    for the same job then coexist, and they need distinct container names, runner names
    and work dirs."""
    return uuid.uuid4().hex[:6]


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
        client: docker.DockerClient | None,
        config: ControllerConfig,
        host: str,
        host_config: HostConfig,
        client_factory: Callable[[str], docker.DockerClient] | None = None,
        suffix_factory: Callable[[], str] = _random_suffix,
    ) -> None:
        # Either a ready client (tests, and the single-host callers) or a factory that
        # builds one on demand. NEVER build it eagerly in __init__: docker.DockerClient
        # contacts the daemon in its constructor (_retrieve_server_version), so an
        # unreachable host would raise before the controller ever reaches its health
        # check — turning "a lane host is asleep" into "the controller crash-loops".
        self._client_or_none = client
        self._client_factory = client_factory
        self._config = config
        self._host = host
        self._host_config = host_config
        self._suffix_factory = suffix_factory

    @property
    def _client(self) -> docker.DockerClient:
        """Build on first use and cache. A failure is NOT cached: the next tick retries,
        which is what lets a lane host that wakes up rejoin the pool on its own."""
        if self._client_or_none is None:
            if self._client_factory is None:
                raise DockerException(f"no docker client or factory for host {self._host!r}")
            self._client_or_none = self._client_factory(self._host_config.docker_endpoint)
        return self._client_or_none

    def spawn(self, decision: AdmitDecision, registration_token: str) -> tuple[str, str]:
        job = decision.job
        # `{host}-cici-{job_id}` keeps a lane greppable back to the job that motivated it;
        # the suffix is what makes the identity the lane's own rather than the job's.
        lane_id = f"{self._host}-cici-{job.job_id}-{self._suffix_factory()}"
        job_class = self._config.classes[decision.class_name]

        # Where the workspace lives decides who reclaims it -- see work_dir_mode in
        # config.py and ADR 0023. Both modes keep the `<lane_id>-work` subdir naming so
        # a workspace path stays greppable back to its lane, and so the entrypoint's EXIT
        # trap always has a removable subdir to `rm -rf` rather than a mount point.
        volumes: dict[str, dict[str, str]] = {}
        mounts: list[DockerMount] = []
        if self._host_config.work_dir_mode == "volume":
            # An ANONYMOUS volume: Source=None is what makes it per-container, so dockerd
            # deletes it alongside the container that auto_remove already removes. Do not
            # give it a name -- a named volume outlives the lane and needs the VOLUMES
            # capability to delete, which the lane-host socket proxy denies.
            #
            # Mounted at /runner-work because the runner image pre-creates that path owned
            # by 1000:1000 (Dockerfile `install -d -o 1000`). That ownership is the whole
            # reason this mode works: a volume inherits the image path's uid, whereas a
            # bind mount to a missing path is created root-owned and locks the uid-1000
            # runner out of its own workspace. Mounting at a path absent from the image
            # would reintroduce exactly that failure, so /runner-work is not arbitrary.
            work_dir = f"/runner-work/{lane_id}-work"
            mounts.append(DockerMount(target="/runner-work", source=None, type="volume"))
        else:
            # Bind the work_dir BASE (pre-created by ansible, owned by the runner uid)
            # rather than a per-lane subdir: if we bind a non-existent per-lane path,
            # docker creates it root-owned and the uid-1000 runner can't write its
            # workspace ("Set up job" fails). Binding the 1000-owned base lets the
            # entrypoint's `mkdir -p` create the per-lane subdir as the runner user.
            work_dirs = self._host_config.work_dirs or {}
            work_base = work_dirs[job_class.work_disk]
            work_dir = f"/runner-base/{lane_id}-work"
            volumes[work_base] = {"bind": "/runner-base", "mode": "rw"}
        # Caches are shared_mounts, NOT work_dirs, so they are unaffected by the mode: a
        # volume-mode lane still gets the same pnpm/gradle/android caches bound in.
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
        # Omitted entirely in bind mode, so powerserver's containers stay byte-identical
        # to before this option existed: docker-py sends an empty Mounts list otherwise.
        if mounts:
            run_kwargs["mounts"] = mounts
        if decision.needs_kvm:
            run_kwargs["devices"] = ["/dev/kvm:/dev/kvm:rwm"]
        if job_class.group_add:
            run_kwargs["group_add"] = list(job_class.group_add)
        # Relative CPU weight, never a cap — see "Resource policy on a machine the
        # operator is using" in the plan. Omitted entirely when None so powerserver's
        # containers stay byte-identical to before per-host config existed.
        if self._host_config.cpu_shares is not None:
            run_kwargs["cpu_shares"] = self._host_config.cpu_shares

        # Per-host image, falling back to the top-level one. A lane host may run an
        # image built without the Android SDK, since its allowed_classes forbid the
        # jobs that need it. _resolve() has already filled this in from the top level
        # when the host omitted it; the `or` is a belt-and-braces default for a
        # HostConfig built directly in a test.
        container = self._client.containers.run(
            self._host_config.runner_image or self._config.runner_image, **run_kwargs
        )
        return lane_id, container.id

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
        # Adapters are cheap and connectionless: the client behind each is built on first
        # use. Constructing the pool must never touch the network, or a single sleeping
        # lane host takes the whole controller down at startup — before any health check
        # can skip it. That is the failure this pool exists to tolerate.
        self._adapters: dict[str, DockerAdapter] = {
            name: DockerAdapter(
                client=None,
                config=config,
                host=name,
                host_config=host_config,
                client_factory=factory,
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
