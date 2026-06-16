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
                }
            ],
        )
    )
    client = GitHubClient(router_url="http://claude-router:8000", github_role="reviewer")
    prs = client.list_open_prs("alvaro/homelab")
    assert prs[0].number == 5
    assert prs[0].head_sha == "head"
    assert route.calls.last.request.headers["Authorization"] == "Bearer ghs_test"
