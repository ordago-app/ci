"""The reviewer mints its own installation tokens from its App's key."""

import json

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from src.github_app import GitHubAppError, GitHubAppTokens

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
).decode()
NOW = 1_800_000_000.0
API = "https://api.github.com"


def _expires(seconds: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(NOW + seconds, UTC).isoformat().replace("+00:00", "Z")


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def _tokens(clock: Clock | None = None) -> GitHubAppTokens:
    return GitHubAppTokens(app_id=42, private_key=PEM, http=httpx.Client(), clock=clock or Clock())


def _routes(installation: int = 7, *tokens: str):
    lookup = respx.get(f"{API}/repos/acme/app/installation").mock(
        return_value=httpx.Response(200, json={"id": installation})
    )
    mint = respx.post(f"{API}/app/installations/{installation}/access_tokens").mock(
        side_effect=[
            httpx.Response(201, json={"token": t, "expires_at": _expires(3600)})
            for t in (tokens or ("ghs_1",))
        ]
    )
    return lookup, mint


@respx.mock
def test_a_token_is_minted_for_that_repo_alone_under_the_apps_own_jwt() -> None:
    lookup, mint = _routes()

    assert _tokens().token("acme/app") == "ghs_1"

    bearer = mint.calls.last.request.headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(
        bearer,
        _KEY.public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == "42"
    assert claims["exp"] - claims["iat"] <= 600
    assert json.loads(mint.calls.last.request.content) == {"repositories": ["app"]}
    assert lookup.call_count == 1


@respx.mock
def test_a_token_is_reused_until_shortly_before_it_expires() -> None:
    _, mint = _routes(7, "ghs_1", "ghs_2")
    clock = Clock()
    tokens = _tokens(clock)

    assert tokens.token("acme/app") == "ghs_1"
    clock.now += 3600 - 6 * 60
    assert tokens.token("acme/app") == "ghs_1"
    clock.now += 2 * 60
    assert tokens.token("acme/app") == "ghs_2"
    assert mint.call_count == 2


@respx.mock
def test_refresh_forgets_the_installation_too() -> None:
    """A reinstall (moving a repo between accounts) changes the installation id."""
    lookup, mint = _routes(7, "ghs_1", "ghs_2")
    tokens = _tokens()
    tokens.token("acme/app")

    assert tokens.token("acme/app", refresh=True) == "ghs_2"
    assert lookup.call_count == 2
    assert mint.call_count == 2


@respx.mock
def test_an_app_not_installed_on_the_repo_says_so() -> None:
    respx.get(f"{API}/repos/acme/app/installation").mock(return_value=httpx.Response(404))

    with pytest.raises(GitHubAppError, match="not installed on acme/app"):
        _tokens().token("acme/app")


def test_an_empty_key_is_refused_at_startup() -> None:
    with pytest.raises(GitHubAppError, match="empty"):
        GitHubAppTokens(app_id=42, private_key="  ")
