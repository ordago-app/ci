from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from .github_app import TokenSource

# A skipped or neutral run does not block a merge; anything else that finished did.
_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


# Authors whose PRs are version bumps: reviewed only where the repo opts in, and
# then with the dependency task rather than the code review.
DEPENDENCY_BOTS = frozenset({"dependabot[bot]", "renovate[bot]"})


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
    labels: list[str] = field(default_factory=list)


class GitHubClient:
    def __init__(self, *, tokens: TokenSource, http: httpx.Client | None = None) -> None:
        self._tokens = tokens
        self._http = http or httpx.Client(timeout=20)

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        body = self._github_get(
            f"https://api.github.com/repos/{repo}/pulls", repo=repo, params={"state": "open"}
        )
        return [self._to_pull_request(item) for item in body]

    def get_pull_request(self, repo: str, number: int) -> PullRequest:
        item = self._github_get(
            f"https://api.github.com/repos/{repo}/pulls/{number}", repo=repo, params={}
        )
        return self._to_pull_request(item)

    def changed_files(self, repo: str, number: int) -> list[str]:
        body = self._github_get(
            f"https://api.github.com/repos/{repo}/pulls/{number}/files", repo=repo, params={}
        )
        return [str(item["filename"]) for item in body]

    def diffstat(self, repo: str, number: int) -> str:
        files = self._github_get(
            f"https://api.github.com/repos/{repo}/pulls/{number}/files", repo=repo, params={}
        )
        changed = len(files)
        additions = sum(int(item.get("additions") or 0) for item in files)
        deletions = sum(int(item.get("deletions") or 0) for item in files)
        return f"{changed} files changed, {additions} insertions(+), {deletions} deletions(-)"

    def ci_summary(self, repo: str, head_sha: str) -> str:
        # CI status is auxiliary prompt context, not the review itself. Without
        # Checks: Read on the reviewer App this endpoint 403s: surface the
        # limitation in the prompt rather than aborting the whole review.
        try:
            runs = self._check_runs(repo, head_sha)
        except httpx.HTTPStatusError as exc:
            return f"CI status unavailable (HTTP {exc.response.status_code})"
        if not runs:
            return "no check runs reported"
        parts = [
            f"{run.get('name')}: {run.get('status')} / {run.get('conclusion')}" for run in runs
        ]
        return "; ".join(parts)

    def head_ci_green(self, repo: str, head_sha: str) -> bool:
        """True when the head has check runs and every one finished without failing.

        Raises on any HTTP error (403 without Checks: Read): the caller decides
        what unreadable CI means."""
        runs = self._check_runs(repo, head_sha)
        return bool(runs) and all(
            run.get("status") == "completed" and run.get("conclusion") in _PASSING_CONCLUSIONS
            for run in runs
        )

    def _check_runs(self, repo: str, head_sha: str) -> list[dict[str, Any]]:
        body = self._github_get(
            f"https://api.github.com/repos/{repo}/commits/{head_sha}/check-runs",
            repo=repo,
            params={"per_page": "100"},
        )
        runs = body.get("check_runs", []) if isinstance(body, dict) else []
        return [run for run in runs if isinstance(run, dict)]

    def post_review(self, repo: str, number: int, body: str, event: str, commit_id: str) -> None:
        resp = self._repo_request(
            "POST",
            f"https://api.github.com/repos/{repo}/pulls/{number}/reviews",
            repo=repo,
            json={"body": body, "event": event, "commit_id": commit_id},
        )
        resp.raise_for_status()

    def post_comment(self, repo: str, number: int, body: str) -> None:
        resp = self._repo_request(
            "POST",
            f"https://api.github.com/repos/{repo}/issues/{number}/comments",
            repo=repo,
            json={"body": body},
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
            labels=[str(label["name"]) for label in item.get("labels", [])],
        )

    def _github_get(self, url: str, *, repo: str, params: dict[str, str]) -> Any:
        resp = self._repo_request("GET", url, repo=repo, params=params)
        resp.raise_for_status()
        return resp.json()

    def _repo_request(self, method: str, url: str, *, repo: str, **kwargs: Any) -> httpx.Response:
        """A repo API call under that repo's installation token, retried once on 401.

        A token is cached for most of its hour, so the cache cannot notice on its
        own that the installation behind it has died. Moving a repo between
        accounts requires uninstalling and reinstalling the App, which revokes the
        live token — and the only signal is the 401 that lands here. So mint
        afresh for this repo and retry, once. A persistently bad credential is
        not something to loop on: the second rejection is surfaced."""
        resp = self._http.request(method, url, headers=self._headers(repo), **kwargs)
        if resp.status_code == 401:
            resp = self._http.request(
                method, url, headers=self._headers(repo, refresh=True), **kwargs
            )
        return resp

    def _headers(self, repo: str, *, refresh: bool = False) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._tokens.token(repo, refresh=refresh)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
