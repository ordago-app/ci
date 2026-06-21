from __future__ import annotations

from dataclasses import dataclass

import docker
import docker.errors

from src.config import ControllerConfig
from src.models import AdmitDecision

LANE_LABEL = "com.homelab.ci-controller.lane"
JOB_LABEL = "com.homelab.ci-controller.job"


@dataclass(frozen=True)
class LaneInfo:
    lane_id: str
    job_id: int
    container_id: str


class DockerAdapter:
    def __init__(self, client: docker.DockerClient, config: ControllerConfig, host: str) -> None:
        self._client = client
        self._config = config
        self._host = host

    def spawn(self, decision: AdmitDecision, registration_token: str) -> str:
        job = decision.job
        lane_id = f"{self._host}-cici-{job.job_id}"
        job_class = self._config.classes[decision.class_name]

        # Bind the work_dir BASE (pre-created by ansible, owned by the runner uid) rather
        # than a per-lane subdir: if we bind a non-existent per-lane path, docker creates
        # it root-owned and the uid-1000 runner can't write its workspace ("Set up job"
        # fails). Binding the 1000-owned base lets the entrypoint's `mkdir -p` create the
        # per-lane subdir as the runner user.
        work_base = self._config.work_dirs[job_class.work_disk]
        work_dir = f"/runner-base/{lane_id}-work"
        volumes: dict[str, dict[str, str]] = {work_base: {"bind": "/runner-base", "mode": "rw"}}
        for mount in self._config.shared_mounts:
            volumes[mount.host] = {"bind": mount.container, "mode": "rw"}

        # Start from the operator-configured lane_env (cache-path parity with the
        # static runner pool — GRADLE_USER_HOME, PNPM_HOME, ANDROID_*), then layer the
        # per-lane RUNNER_* on top so they always win.
        environment = dict(self._config.lane_env)
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
            "network": "homelab",
            "environment": environment,
            "volumes": volumes,
            "labels": {LANE_LABEL: lane_id, JOB_LABEL: str(job.job_id)},
        }
        if decision.needs_kvm:
            run_kwargs["devices"] = ["/dev/kvm:/dev/kvm:rwm"]
        if job_class.group_add:
            run_kwargs["group_add"] = list(job_class.group_add)

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
                )
            )
        return lanes

    def remove(self, container_id: str) -> None:
        try:
            container = self._client.containers.get(container_id)
            container.remove(force=True)
        except docker.errors.NotFound:
            pass
