import httpx
import pytest
from src.review_client import ReviewStateClient


def _client(handler) -> ReviewStateClient:
    return ReviewStateClient(
        "http://github-review:8000/", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_head_states_asks_once_for_every_distinct_head() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"repo": "o/r", "heads": {"a": "approved"}})

    assert _client(handler).head_states("o/r", ["b", "a", "b"]) == {"a": "approved"}
    (url,) = seen
    assert url.path == "/heads"
    assert url.params.get_list("sha") == ["a", "b"]
    assert url.params["repo"] == "o/r"


def test_head_states_raises_when_github_review_errors() -> None:
    """The controller turns this into untiered admission plus an ERROR log; the client
    must not paper over it with an empty answer."""
    with pytest.raises(httpx.HTTPStatusError):
        _client(lambda request: httpx.Response(503)).head_states("o/r", ["a"])
