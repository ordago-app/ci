"""Installation tokens for the reviewer's own GitHub App, minted in-process.

The service used to borrow tokens from a router service on its operator's
network. Minting them here, from the App's key, is what lets an instance run on
any Docker host: the only thing it needs from outside is the key.

Each token is scoped to the one repository it is asked for. The App is usually
installed on an account owning several reviewed repos, and a token for one of
them has no business reaching the rest.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import httpx
import jwt

_API = "https://api.github.com"
_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
# Re-mint this long before GitHub's expiry, so a token handed to a long git
# fetch or a review post never expires mid-request.
_EXPIRY_MARGIN_SECONDS = 5 * 60


class TokenSource(Protocol):
    def token(self, repo: str, *, refresh: bool = False) -> str: ...


class GitHubAppError(RuntimeError):
    pass


class GitHubAppTokens:
    def __init__(
        self,
        *,
        app_id: int,
        private_key: str,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not private_key.strip():
            raise GitHubAppError(f"GitHub App {app_id}: the private key is empty")
        self._app_id = app_id
        self._private_key = private_key
        self._http = http or httpx.Client(timeout=20)
        self._clock = clock
        self._lock = threading.Lock()
        self._installations: dict[str, int] = {}
        self._tokens: dict[str, tuple[str, float]] = {}

    def token(self, repo: str, *, refresh: bool = False) -> str:
        """A token for `repo`, reused until shortly before it expires.

        `refresh` forgets what is cached for `repo` — its token and its
        installation id — and mints anew. That is the answer to a 401: moving a
        repo between accounts reinstalls the App, which revokes the live token
        and changes the installation id, and the 401 is the only signal."""
        with self._lock:
            if refresh:
                self._tokens.pop(repo, None)
                self._installations.pop(repo, None)
            cached = self._tokens.get(repo)
            if cached is not None and cached[1] - _EXPIRY_MARGIN_SECONDS > self._clock():
                return cached[0]
            installation = self._installations.get(repo)
            if installation is None:
                installation = self._installation_for(repo)
                self._installations[repo] = installation
            token, expires_at = self._mint(installation, repo)
            self._tokens[repo] = (token, expires_at)
            return token

    def _app_jwt(self) -> str:
        now = int(self._clock())
        # iat backdated a minute for clock skew; GitHub caps exp at ten minutes.
        claims = {"iat": now - 60, "exp": now + 9 * 60, "iss": str(self._app_id)}
        return jwt.encode(claims, self._private_key, algorithm="RS256")

    def _installation_for(self, repo: str) -> int:
        resp = self._http.get(
            f"{_API}/repos/{repo}/installation",
            headers={**_HEADERS, "Authorization": f"Bearer {self._app_jwt()}"},
        )
        if resp.status_code == 404:
            raise GitHubAppError(
                f"GitHub App {self._app_id} is not installed on {repo}: install it on the "
                "account that owns the repo"
            )
        resp.raise_for_status()
        return int(resp.json()["id"])

    def _mint(self, installation: int, repo: str) -> tuple[str, float]:
        resp = self._http.post(
            f"{_API}/app/installations/{installation}/access_tokens",
            headers={**_HEADERS, "Authorization": f"Bearer {self._app_jwt()}"},
            json={"repositories": [repo.split("/", 1)[1]]},
        )
        resp.raise_for_status()
        body = resp.json()
        expires = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00"))
        return str(body["token"]), expires.timestamp()
