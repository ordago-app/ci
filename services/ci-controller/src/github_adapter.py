from __future__ import annotations

import datetime as dt
import time

import httpx
import jwt

from src.models import QueuedJob

_API_VERSION = "2026-03-10"


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
                        )
                    )
        return jobs
