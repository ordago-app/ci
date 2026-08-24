import httpx
import respx
from src.github_client import GitHubClient


@respx.mock
def test_fetches_token_from_router_and_lists_open_prs() -> None:
    respx.get("http://claude-router:8000/internal/github-token").mock(
        return_value=httpx.Response(200, json={"token": "ghs_test", "expires_at": 123.0})
    )
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
    client = GitHubClient(router_url="http://claude-router:8000", github_role="reviewer")
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
    respx.get("http://claude-router:8000/internal/github-token").mock(
        return_value=httpx.Response(200, json={"token": "ghs_test", "expires_at": 123.0})
    )
    respx.get("https://api.github.com/repos/alvaro/homelab/commits/deadbeef/check-runs").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible by integration"})
    )
    client = GitHubClient(router_url="http://claude-router:8000", github_role="reviewer")
    summary = client.ci_summary("alvaro/homelab", "deadbeef")
    assert "unavailable" in summary.lower()
    assert "403" in summary


@respx.mock
def test_post_review_sends_commit_id() -> None:
    respx.get("http://claude-router:8000/internal/github-token").mock(
        return_value=httpx.Response(200, json={"token": "ghs_test", "expires_at": 123.0})
    )
    route = respx.post("https://api.github.com/repos/alvaro/homelab/pulls/1/reviews").mock(
        return_value=httpx.Response(200, json={})
    )
    client = GitHubClient(router_url="http://claude-router:8000", github_role="reviewer")
    client.post_review("alvaro/homelab", 1, "Looks good.", "COMMENT", commit_id="deadbeef")
    import json

    sent = json.loads(route.calls.last.request.content)
    assert sent["commit_id"] == "deadbeef"
    assert sent["body"] == "Looks good."
    assert sent["event"] == "COMMENT"


TOKEN_URL = "http://claude-router:8000/internal/github-token"


def _client() -> GitHubClient:
    return GitHubClient(router_url="http://claude-router:8000", github_role="reviewer")


@respx.mock
def test_token_request_names_the_repo_being_acted_on() -> None:
    """The reviewer App has a different installation id under every account that
    owns a reviewed repo, so the router cannot pick one without being told."""
    token = respx.get(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"token": "ghs_test", "expires_at": 123.0})
    )
    respx.get("https://api.github.com/repos/acme/ordago-apps/pulls").mock(
        return_value=httpx.Response(200, json=[])
    )
    _client().list_open_prs("acme/ordago-apps")
    params = token.calls.last.request.url.params
    assert params["repo"] == "acme/ordago-apps"
    assert params["role"] == "reviewer"


@respx.mock
def test_each_repo_gets_a_token_minted_for_its_own_owner() -> None:
    """One process polls several owners; two repos must not share a token."""
    token = respx.get(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"token": "ghs_test", "expires_at": 123.0})
    )
    respx.get("https://api.github.com/repos/acme/ordago-apps/pulls").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get("https://api.github.com/repos/personal/homelab/pulls").mock(
        return_value=httpx.Response(200, json=[])
    )
    client = _client()
    client.list_open_prs("acme/ordago-apps")
    client.list_open_prs("personal/homelab")
    asked = [c.request.url.params["repo"] for c in token.calls]
    assert asked == ["acme/ordago-apps", "personal/homelab"]


@respx.mock
def test_a_401_refreshes_the_cached_installation_and_retries_once() -> None:
    """Moving a repo between accounts revokes a token the router has cached for
    its full hour. The 401 lands here, so this is the only place that can tell
    the router to drop it — otherwise every call fails for the rest of the hour,
    which is exactly the hour the operator is watching."""
    token = respx.get(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"token": "ghs_test", "expires_at": 123.0})
    )
    prs = respx.get("https://api.github.com/repos/acme/ordago-apps/pulls").mock(
        side_effect=[
            httpx.Response(401, json={"message": "Bad credentials"}),
            httpx.Response(200, json=[]),
        ]
    )
    assert _client().list_open_prs("acme/ordago-apps") == []
    assert prs.call_count == 2
    assert token.calls[0].request.url.params.get("refresh") is None
    assert token.calls[1].request.url.params["refresh"] == "true"


@respx.mock
def test_a_persistent_401_is_surfaced_not_retried_forever() -> None:
    respx.get(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"token": "ghs_test", "expires_at": 123.0})
    )
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
    respx.get(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"token": "ghs_test", "expires_at": 123.0})
    )
    route = respx.post("https://api.github.com/repos/acme/ordago-apps/pulls/1/reviews").mock(
        side_effect=[httpx.Response(401, json={}), httpx.Response(200, json={})]
    )
    _client().post_review("acme/ordago-apps", 1, "ok", "COMMENT", commit_id="deadbeef")
    assert route.call_count == 2
