import httpx
import pytest
import respx
from src.github_client import GitHubClient


class FakeTokens:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def token(self, repo: str, *, refresh: bool = False) -> str:
        self.calls.append((repo, refresh))
        return "ghs_test"


@respx.mock
def test_lists_open_prs_under_the_repos_token() -> None:
    route = respx.get("https://api.github.com/repos/alvaro/homelab/pulls").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 5,
                    "title": "Change backup",
                    "body": "Body",
                    "draft": False,
                    "state": "open",
                    "user": {"login": "alice"},
                    "base": {"ref": "main", "sha": "base"},
                    "head": {"ref": "feat", "sha": "head"},
                    "labels": [{"name": "ai-review"}, {"name": "wip"}],
                }
            ],
        )
    )
    client = GitHubClient(tokens=FakeTokens())
    prs = client.list_open_prs("alvaro/homelab")
    assert prs[0].number == 5
    assert prs[0].head_sha == "head"
    assert prs[0].labels == ["ai-review", "wip"]
    assert route.calls.last.request.headers["Authorization"] == "Bearer ghs_test"


@respx.mock
def test_ci_summary_degrades_when_checks_not_readable() -> None:
    # Regression: the reviewer App has no `Checks: Read` permission, so the
    # check-runs endpoint 403s. CI status is auxiliary prompt context — a review
    # must not abort over it. Surface the limitation instead of raising.
    respx.get("https://api.github.com/repos/alvaro/homelab/commits/deadbeef/check-runs").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible by integration"})
    )
    client = GitHubClient(tokens=FakeTokens())
    summary = client.ci_summary("alvaro/homelab", "deadbeef")
    assert "unavailable" in summary.lower()
    assert "403" in summary


@respx.mock
def test_post_review_sends_commit_id() -> None:
    route = respx.post("https://api.github.com/repos/alvaro/homelab/pulls/1/reviews").mock(
        return_value=httpx.Response(200, json={})
    )
    client = GitHubClient(tokens=FakeTokens())
    client.post_review("alvaro/homelab", 1, "Looks good.", "COMMENT", commit_id="deadbeef")
    import json

    sent = json.loads(route.calls.last.request.content)
    assert sent["commit_id"] == "deadbeef"
    assert sent["body"] == "Looks good."
    assert sent["event"] == "COMMENT"


def _client(tokens: FakeTokens | None = None) -> GitHubClient:
    return GitHubClient(tokens=tokens or FakeTokens())


@respx.mock
def test_each_repo_is_called_with_a_token_for_that_repo() -> None:
    """One process polls several owners; two repos must not share a token."""
    respx.get("https://api.github.com/repos/acme/ordago-apps/pulls").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get("https://api.github.com/repos/personal/homelab/pulls").mock(
        return_value=httpx.Response(200, json=[])
    )
    tokens = FakeTokens()
    client = _client(tokens)
    client.list_open_prs("acme/ordago-apps")
    client.list_open_prs("personal/homelab")
    assert tokens.calls == [("acme/ordago-apps", False), ("personal/homelab", False)]


@respx.mock
def test_a_401_mints_afresh_and_retries_once() -> None:
    """Moving a repo between accounts revokes a token cached for most of its
    hour. The 401 lands here, so this is the only place that can drop it —
    otherwise every call fails for the rest of the hour."""
    prs = respx.get("https://api.github.com/repos/acme/ordago-apps/pulls").mock(
        side_effect=[
            httpx.Response(401, json={"message": "Bad credentials"}),
            httpx.Response(200, json=[]),
        ]
    )
    tokens = FakeTokens()
    assert _client(tokens).list_open_prs("acme/ordago-apps") == []
    assert prs.call_count == 2
    assert tokens.calls == [("acme/ordago-apps", False), ("acme/ordago-apps", True)]


@respx.mock
def test_a_persistent_401_is_surfaced_not_retried_forever() -> None:
    prs = respx.get("https://api.github.com/repos/acme/ordago-apps/pulls").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"})
    )
    try:
        _client().list_open_prs("acme/ordago-apps")
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 401
    else:  # pragma: no cover - the assertion below reports it
        raise AssertionError("a persistently rejected credential must surface")
    assert prs.call_count == 2


@respx.mock
def test_post_review_also_retries_once_on_401() -> None:
    """post_review is the one call that matters most: a review lost to a stale
    token is a PR that silently never gets one."""
    route = respx.post("https://api.github.com/repos/acme/ordago-apps/pulls/1/reviews").mock(
        side_effect=[httpx.Response(401, json={}), httpx.Response(200, json={})]
    )
    _client().post_review("acme/ordago-apps", 1, "ok", "COMMENT", commit_id="deadbeef")
    assert route.call_count == 2


@respx.mock
def test_post_comment_goes_to_the_pr_conversation() -> None:
    route = respx.post("https://api.github.com/repos/acme/ordago-apps/issues/7/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    _client().post_comment("acme/ordago-apps", 7, "no review")
    assert route.call_count == 1
    assert route.calls.last.request.content == b'{"body":"no review"}'


def _check_runs(*runs: tuple[str, str | None]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "total_count": len(runs),
            "check_runs": [
                {"name": f"job{i}", "status": status, "conclusion": conclusion}
                for i, (status, conclusion) in enumerate(runs)
            ],
        },
    )


@respx.mock
def test_head_ci_green_needs_every_run_finished_and_passing() -> None:
    url = "https://api.github.com/repos/alvaro/homelab/commits/{}/check-runs"
    respx.get(url.format("green")).mock(
        return_value=_check_runs(
            ("completed", "success"), ("completed", "skipped"), ("completed", "neutral")
        )
    )
    respx.get(url.format("running")).mock(
        return_value=_check_runs(("completed", "success"), ("in_progress", None))
    )
    respx.get(url.format("red")).mock(
        return_value=_check_runs(("completed", "success"), ("completed", "failure"))
    )
    respx.get(url.format("none")).mock(return_value=_check_runs())
    client = GitHubClient(tokens=FakeTokens())

    assert client.head_ci_green("alvaro/homelab", "green")
    assert not client.head_ci_green("alvaro/homelab", "running")
    assert not client.head_ci_green("alvaro/homelab", "red")
    assert not client.head_ci_green("alvaro/homelab", "none"), "no CI reported is not green"


@respx.mock
def test_head_ci_green_raises_when_checks_are_not_readable() -> None:
    respx.get("https://api.github.com/repos/alvaro/homelab/commits/deadbeef/check-runs").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible by integration"})
    )
    client = GitHubClient(tokens=FakeTokens())
    with pytest.raises(httpx.HTTPStatusError):
        client.head_ci_green("alvaro/homelab", "deadbeef")
