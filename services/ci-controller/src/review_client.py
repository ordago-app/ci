from __future__ import annotations

import httpx

# One attempt, short: the tick is the retry, and a github-review that hangs must cost
# the admission loop seconds, not the 30 s a GitHub call is allowed.
REVIEW_TIMEOUT_SECONDS = 3.0


class ReviewStateClient:
    """Reads per-head review state from github-review's GET /heads (store-only there,
    so polling it every tick costs no GitHub call)."""

    def __init__(self, base_url: str, *, client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=REVIEW_TIMEOUT_SECONDS)

    def head_states(self, repo: str, head_shas: list[str]) -> dict[str, str]:
        """{head_sha: state} for the heads github-review has a row for; others absent."""
        resp = self._client.get(
            f"{self._base_url}/heads", params={"repo": repo, "sha": sorted(set(head_shas))}
        )
        resp.raise_for_status()
        heads = resp.json()["heads"]
        return {str(sha): str(state) for sha, state in heads.items()}
