from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    draft: bool
    state: str
    author: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str


class GitHubClient:
    def __init__(
        self,
        *,
        router_url: str,
        github_role: str,
        http: httpx.Client | None = None,
    ) -> None:
        self._router_url = router_url.rstrip("/")
        self._github_role = github_role
        self._http = http or httpx.Client(timeout=20)

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        body = self._github_get(
            f"https://api.github.com/repos/{repo}/pulls", params={"state": "open"}
        )
        return [self._to_pull_request(item) for item in body]

    def get_pull_request(self, repo: str, number: int) -> PullRequest:
        item = self._github_get(f"https://api.github.com/repos/{repo}/pulls/{number}", params={})
        return self._to_pull_request(item)

    def changed_files(self, repo: str, number: int) -> list[str]:
        body = self._github_get(
            f"https://api.github.com/repos/{repo}/pulls/{number}/files", params={}
        )
        return [str(item["filename"]) for item in body]

    def diffstat(self, repo: str, number: int) -> str:
        files = self._github_get(
            f"https://api.github.com/repos/{repo}/pulls/{number}/files", params={}
        )
        changed = len(files)
        additions = sum(int(item.get("additions") or 0) for item in files)
        deletions = sum(int(item.get("deletions") or 0) for item in files)
        return f"{changed} files changed, {additions} insertions(+), {deletions} deletions(-)"

    def ci_summary(self, repo: str, head_sha: str) -> str:
        # CI status is auxiliary prompt context, not the review itself. The
        # reviewer App is intentionally minimal (Pull requests / Contents /
        # Metadata) and lacks Checks: Read, so this endpoint 403s. Surface the
        # limitation in the prompt rather than aborting the whole review.
        try:
            body = self._github_get(
                f"https://api.github.com/repos/{repo}/commits/{head_sha}/check-runs", params={}
            )
        except httpx.HTTPStatusError as exc:
            return f"CI status unavailable (HTTP {exc.response.status_code})"
        runs = body.get("check_runs", []) if isinstance(body, dict) else []
        if not runs:
            return "no check runs reported"
        parts = [
            f"{run.get('name')}: {run.get('status')} / {run.get('conclusion')}" for run in runs
        ]
        return "; ".join(parts)

    def post_review(self, repo: str, number: int, body: str, event: str, commit_id: str) -> None:
        token = self._token()
        resp = self._http.post(
            f"https://api.github.com/repos/{repo}/pulls/{number}/reviews",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"body": body, "event": event, "commit_id": commit_id},
        )
        resp.raise_for_status()

    def _to_pull_request(self, item: dict[str, Any]) -> PullRequest:
        return PullRequest(
            number=int(item["number"]),
            title=str(item.get("title") or ""),
            body=str(item.get("body") or ""),
            draft=bool(item.get("draft")),
            state=str(item.get("state") or ""),
            author=str(item["user"]["login"]),
            base_ref=str(item["base"]["ref"]),
            base_sha=str(item["base"]["sha"]),
            head_ref=str(item["head"]["ref"]),
            head_sha=str(item["head"]["sha"]),
        )

    def _github_get(self, url: str, *, params: dict[str, str]) -> Any:
        token = self._token()
        resp = self._http.get(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        return resp.json()

    def _token(self) -> str:
        resp = self._http.get(
            f"{self._router_url}/internal/github-token",
            params={"role": self._github_role},
        )
        resp.raise_for_status()
        return str(resp.json()["token"])
