from __future__ import annotations

from dataclasses import dataclass

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
    def __init__(self, client: object, config: ControllerConfig, host: str) -> None:
        self._client = client
        self._config = config
        self._host = host

    def spawn(self, decision: AdmitDecision, registration_token: str) -> str:
        job = decision.job
        lane_id = f"{self._host}-cici-{job.job_id}"
        job_class = self._config.classes[decision.class_name]

        work_base = self._config.work_dirs[job_class.work_disk]
        work_host = f"{work_base}/{lane_id}-work"
        volumes: dict[str, dict[str, str]] = {work_host: {"bind": "/runner-work", "mode": "rw"}}
        for mount in self._config.shared_mounts:
            volumes[mount.host] = {"bind": mount.container, "mode": "rw"}

        environment = {
            "RUNNER_REPOSITORY": job.repo,
            "RUNNER_NAME": lane_id,
            "RUNNER_LABELS": ",".join(job.labels),
            "RUNNER_WORKDIR": "/runner-work",
            "RUNNER_EPHEMERAL": "1",
            "RUNNER_REGISTRATION_TOKEN": registration_token,
            "SKIP_ANDROID_SDK": "0" if job_class.needs_android_sdk else "1",
        }

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
        if job_class.needs_kvm:
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
