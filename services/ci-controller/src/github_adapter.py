from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass

import httpx
import jwt

from src.models import QueuedJob, RunningJob

log = logging.getLogger("ci-controller")

_API_VERSION = "2026-03-10"

RUNNER_PAGE_SIZE = 100
# A repo would need >1000 registered runners to hit this. The bound exists so a
# pathological (or corrupted) listing cannot spend the installation token's hourly
# budget on one tick; hitting it yields an explicitly incomplete listing, not a
# silently short one.
MAX_RUNNER_PAGES = 10

# How many in_progress runs find_job_for_runner scans for a runner name. Deliberately
# bounded (1 + N calls per attribution, against a 5000 req/hr installation token): a
# lane whose run sits past this page stays unattributed, which the controller warns
# about rather than paying unbounded pagination to avoid.
RUNS_SCAN_LIMIT = 50

# GitHub paginates /runs/{id}/jobs at 30 by default. A matrix job can easily exceed that,
# and the self-hosted leg is not reliably on the first page — asking without per_page or
# pagination left such a lane permanently unattributed. 100 is the API maximum.
JOBS_PAGE_SIZE = 100
# Same reasoning as MAX_RUNNER_PAGES: bounded so one pathological run cannot spend the
# installation token's hourly budget, and hitting the bound yields an explicitly
# incomplete scan rather than a silently short one.
MAX_JOBS_PAGES = 5


@dataclass(frozen=True)
class RunnerInfo:
    runner_id: int
    name: str
    busy: bool


@dataclass(frozen=True)
class RunnerList:
    """A repo's registered runners, and whether the listing is known to be complete.

    Completeness is load-bearing: the idle reaper force-removes an unregistered lane on
    the strength of it being ABSENT from this list. Absence read off a truncated listing
    would tear down a lane that is registered and executing a job."""

    runners: list[RunnerInfo]
    complete: bool


@dataclass(frozen=True)
class JobLookup:
    """The outcome of scanning for the job a named runner is executing.

    `complete` is load-bearing for the same reason it is on RunnerList: a bare None
    conflated "this runner is genuinely running nothing we can see" with "we stopped
    looking". The caller retries on the first and must say so out loud on the second,
    or a truncated scan looks exactly like a run that has not appeared yet and the lane
    stays booting forever with nobody able to tell why.
    """

    job: RunningJob | None
    complete: bool


class GitHubAdapter:
    def __init__(
        self,
        app_id: str,
        installation_id: str,
        private_key_pem: str,
        *,
        base_url: str = "https://api.github.com",
        client: httpx.Client | None = None,
    ) -> None:
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key = private_key_pem
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30)
        self._inst_token: str | None = None
        self._inst_expiry: float = 0.0

    def _headers(self, bearer: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {bearer}",
            "X-GitHub-Api-Version": _API_VERSION,
        }

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def installation_token(self) -> str:
        if self._inst_token is not None and time.time() < self._inst_expiry - 60:
            return self._inst_token
        resp = self._client.post(
            f"{self._base_url}/app/installations/{self._installation_id}/access_tokens",
            headers=self._headers(self._app_jwt()),
        )
        resp.raise_for_status()
        data = resp.json()
        self._inst_token = str(data["token"])
        self._inst_expiry = dt.datetime.fromisoformat(
            data["expires_at"].replace("Z", "+00:00")
        ).timestamp()
        return self._inst_token

    def mint_registration_token(self, repo: str) -> str:
        resp = self._client.post(
            f"{self._base_url}/repos/{repo}/actions/runners/registration-token",
            headers=self._headers(self.installation_token()),
        )
        resp.raise_for_status()
        return str(resp.json()["token"])

    def job_conclusion(self, repo: str, job_id: int) -> str | None:
        """Terminal conclusion of a job, or None if it never reached one."""
        resp = self._client.get(
            f"{self._base_url}/repos/{repo}/actions/jobs/{job_id}",
            headers=self._headers(self.installation_token()),
        )
        resp.raise_for_status()
        conclusion = resp.json().get("conclusion")
        return str(conclusion) if conclusion else None

    def list_queued_jobs(self, repo: str) -> list[QueuedJob]:
        headers = self._headers(self.installation_token())
        runs_resp = self._client.get(
            f"{self._base_url}/repos/{repo}/actions/runs",
            params={"status": "queued", "per_page": 50},
            headers=headers,
        )
        runs_resp.raise_for_status()
        jobs: list[QueuedJob] = []
        for run in runs_resp.json().get("workflow_runs", []):
            jobs_resp = self._client.get(
                f"{self._base_url}/repos/{repo}/actions/runs/{run['id']}/jobs",
                headers=headers,
            )
            jobs_resp.raise_for_status()
            for job in jobs_resp.json().get("jobs", []):
                if job.get("status") == "queued":
                    jobs.append(
                        QueuedJob(
                            job_id=int(job["id"]),
                            repo=repo,
                            labels=[str(label) for label in job.get("labels", [])],
                            workflow=str(run.get("name") or ""),
                            job_name=str(job.get("name") or ""),
                        )
                    )
        return jobs

    def list_runners(self, repo: str) -> RunnerList:
        """Registered self-hosted runners. RUNNER_NAME is the lane_id, so this joins to
        the ledger by name. Normally one call per repo per tick — the cheap half of lane
        state — and more only when a repo genuinely has over 100 runners registered
        (a static pool plus ephemeral lanes plus not-yet-purged offline registrations)."""
        headers = self._headers(self.installation_token())
        collected: list[RunnerInfo] = []
        for page in range(1, MAX_RUNNER_PAGES + 1):
            resp = self._client.get(
                f"{self._base_url}/repos/{repo}/actions/runners",
                params={"per_page": RUNNER_PAGE_SIZE, "page": page},
                headers=headers,
            )
            resp.raise_for_status()
            body = resp.json()
            batch = body.get("runners", [])
            collected.extend(
                RunnerInfo(runner_id=int(r["id"]), name=str(r["name"]), busy=bool(r.get("busy")))
                for r in batch
            )
            total = body.get("total_count")
            seen_all = total is not None and len(collected) >= int(total)
            if len(batch) < RUNNER_PAGE_SIZE or seen_all:
                return RunnerList(runners=collected, complete=True)
        log.warning(
            "runner list for %s is truncated at %s runners (%s pages): callers must not "
            "read a lane's absence from it",
            repo,
            len(collected),
            MAX_RUNNER_PAGES,
        )
        return RunnerList(runners=collected, complete=False)

    def delete_runner(self, repo: str, runner_id: int) -> bool:
        """Deregister a runner. False means GitHub refused because it is busy (422) — it
        was handed a job since we last polled, so the caller must not tear it down. 404
        means the registration is already gone, which is the outcome we wanted anyway."""
        resp = self._client.delete(
            f"{self._base_url}/repos/{repo}/actions/runners/{runner_id}",
            headers=self._headers(self.installation_token()),
        )
        if resp.status_code == 422:
            return False
        if resp.status_code == 404:
            return True
        resp.raise_for_status()
        return True

    def find_job_for_runner(self, repo: str, runner_name: str) -> JobLookup:
        """The expensive half of lane state (1+N calls), so callers must spend it only on an
        idle->busy transition. Runners are ephemeral, so that happens at most once per lane.

        A miss is only reported as complete when every run's job list was read to its end;
        if any run's jobs were cut short at MAX_JOBS_PAGES the whole scan is incomplete,
        because the job we were looking for could have been on the page we did not fetch.
        """
        headers = self._headers(self.installation_token())
        runs_resp = self._client.get(
            f"{self._base_url}/repos/{repo}/actions/runs",
            params={"status": "in_progress", "per_page": RUNS_SCAN_LIMIT},
            headers=headers,
        )
        runs_resp.raise_for_status()
        complete = True
        for run in runs_resp.json().get("workflow_runs", []):
            for page in range(1, MAX_JOBS_PAGES + 1):
                jobs_resp = self._client.get(
                    f"{self._base_url}/repos/{repo}/actions/runs/{run['id']}/jobs",
                    params={"per_page": JOBS_PAGE_SIZE, "page": page},
                    headers=headers,
                )
                jobs_resp.raise_for_status()
                body = jobs_resp.json()
                batch = body.get("jobs", [])
                for job in batch:
                    if job.get("runner_name") == runner_name and job.get("status") == "in_progress":
                        return JobLookup(
                            job=RunningJob(
                                job_id=int(job["id"]),
                                workflow=str(run.get("name") or ""),
                                job_name=str(job.get("name") or ""),
                            ),
                            complete=True,
                        )
                total = body.get("total_count")
                seen_all = total is not None and page * JOBS_PAGE_SIZE >= int(total)
                if len(batch) < JOBS_PAGE_SIZE or seen_all:
                    break
            else:
                log.warning(
                    "jobs scan for run %s in %s is truncated at %s pages: a lane's job may "
                    "sit past it, so this scan cannot be read as 'runner has no job'",
                    run.get("id"),
                    repo,
                    MAX_JOBS_PAGES,
                )
                complete = False
        return JobLookup(job=None, complete=complete)
