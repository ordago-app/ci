from __future__ import annotations

import logging

from src.admission import evaluate
from src.config import ControllerConfig
from src.docker_adapter import DockerAdapter
from src.github_adapter import GitHubAdapter
from src.ledger import Ledger
from src.models import AdmitDecision, Decision, DeferDecision, QueuedJob, Reservation

log = logging.getLogger("ci-controller")


class Controller:
    def __init__(
        self,
        config: ControllerConfig,
        github: GitHubAdapter,
        docker: DockerAdapter,
        ledger: Ledger,
    ) -> None:
        self.config = config
        self.github = github
        self.docker = docker
        self.ledger = ledger
        self._last_decisions: list[Decision] = []

    def reconcile(self) -> None:
        running = {lane.lane_id: lane for lane in self.docker.list_lanes()}
        # Drop ledger entries whose lane is gone (frees budget).
        for lane_id in self.ledger.lane_ids() - set(running):
            self.ledger.remove(lane_id)
        # Re-adopt running lanes the ledger doesn't know about (post-restart).
        known_jobs = {r.job_id for r in self.ledger.reservations()}
        for lane in running.values():
            if lane.job_id in known_jobs:
                continue
            self._readopt(lane.lane_id, lane.job_id)

    def _readopt(self, lane_id: str, job_id: int) -> None:
        # Best-effort: we cannot recover the repo/labels from the lane label set,
        # so reserve the default class's footprint (a safe over- or exact-estimate
        # for the common light lane). Heavy lanes self-correct when they finish.
        job_class = self.config.classes[self.config.default_class]
        self.ledger.add(
            Reservation(
                lane_id=lane_id,
                job_id=job_id,
                repo="(adopted)",
                class_name=self.config.default_class,
                ram_mb=job_class.ram_mb,
                needs_kvm=job_class.needs_kvm,
            )
        )

    def tick(self) -> list[Decision]:
        self.reconcile()
        jobs: list[QueuedJob] = []
        for repo in self.config.repo_names():
            try:
                jobs.extend(self.github.list_queued_jobs(repo))
            except Exception as exc:  # noqa: BLE001 — one bad repo must not kill the loop
                log.warning("poll failed for %s: %s", repo, exc)

        decisions = evaluate(jobs, self.ledger, self.config)
        for decision in decisions:
            if isinstance(decision, AdmitDecision):
                self._admit(decision)
        self._last_decisions = decisions
        return decisions

    def _admit(self, decision: AdmitDecision) -> None:
        try:
            token = self.github.mint_registration_token(decision.job.repo)
            lane_id = self.docker.spawn(decision, registration_token=token)
        except Exception as exc:  # noqa: BLE001 — spawn failure: leave job queued
            log.error("failed to spawn lane for job %s: %s", decision.job.job_id, exc)
            return
        self.ledger.add(
            Reservation(
                lane_id=lane_id,
                job_id=decision.job.job_id,
                repo=decision.job.repo,
                class_name=decision.class_name,
                ram_mb=decision.ram_mb,
                needs_kvm=decision.needs_kvm,
            )
        )
        log.info("admitted job %s (%s) on lane %s", decision.job.job_id, decision.class_name, lane_id)

    def status(self) -> dict:
        deferred = [d for d in self._last_decisions if isinstance(d, DeferDecision)]
        return {
            "budget_ram_mb": self.config.ram_budget_mb,
            "ledger_ram_mb": self.ledger.total_ram(),
            "lanes_running": self.ledger.lane_count(),
            "max_lanes": self.config.max_concurrent_lanes,
            "kvm_in_use": self.ledger.kvm_in_use(),
            "running": [
                {
                    "lane_id": r.lane_id,
                    "job_id": r.job_id,
                    "repo": r.repo,
                    "class": r.class_name,
                    "ram_mb": r.ram_mb,
                }
                for r in self.ledger.reservations()
            ],
            "deferred": [{"job_id": d.job.job_id, "repo": d.job.repo, "reason": d.reason} for d in deferred],
        }
