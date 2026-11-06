"""The one place in github-review that may refresh the shared Codex login.

A codex account's `auth.json` holds a ChatGPT OAuth login whose refresh
token is single-use: each refresh returns a new one and retires the old. The
codex CLI refreshes on its own, proactively when the access token is within
five minutes of expiry and reactively on a 401, and its refresh lock is
per-process. Two concurrent review sessions sharing that file can both refresh
with the same token; the loser gets `refresh_token_reused` and the account is
logged out until someone logs in again (`codex login`) and replaces the file.

So no session holds a refresh token. `CodexAuthOwner` refreshes the shared file
itself, serialised, whenever the access token would not outlive a review, and
hands each session a copy with the refresh token blanked. A session that still
tried to refresh would fail its own review rather than spend the shared token.

It must be the only refresher of that login anywhere: no other process, and no
second reviewer instance, may refresh the same `auth.json` (the config refuses
two accounts on one file; two instances it cannot see, so give each instance a
login of its own). Other consumers of a login this service owns must run on a
refresh-less copy of it. `keep_fresh` keeps its access token valid
`KEEP_FRESH_HORIZON` ahead even when no review runs, so such copies are usable
whenever they are taken.

The refresh request is the codex CLI's own (`codex-rs/login/src/auth/manager.rs`:
JSON grant to `REFRESH_TOKEN_URL` with its public `CLIENT_ID`), and the file is
written back in the shape the CLI writes, so every other reader of the file
keeps working.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ..provider import ProviderUnavailable

REFRESH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
# The CLI's fallback when the access token carries no readable `exp`.
TOKEN_REFRESH_INTERVAL = timedelta(days=8)
KEEP_FRESH_HORIZON = timedelta(hours=2)
KEEP_FRESH_INTERVAL = timedelta(minutes=15)

_RELOGIN = "log in again with `codex login` and replace the file with its auth.json"

log = logging.getLogger(__name__)


class CodexAuthOwner:
    def __init__(self, auth_file: Path, account: str, http: httpx.Client | None = None) -> None:
        self._auth_file = auth_file
        self._account = account
        self._http = http or httpx.Client(timeout=30)
        self._lock = threading.Lock()

    def write_session_copy(self, dest: Path, valid_for_seconds: float) -> None:
        """Write a refresh-less copy of the login to `dest`, mode 0400, refreshing
        the shared file first if its access token would expire within
        `valid_for_seconds`. The caller owns `dest` from then on, and deletes it."""
        auth = self._ensure_fresh(valid_for_seconds)
        session_auth = copy.deepcopy(auth)
        session_auth["tokens"]["refresh_token"] = ""
        dest.unlink(missing_ok=True)
        # Owner-read-only from the first byte: it still holds a live access token.
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(fd, "w") as out:
            out.write(json.dumps(session_auth, indent=2))

    def keep_fresh(self, stop: threading.Event) -> None:
        """Until `stop` is set, refresh the shared login whenever its access token
        would expire within KEEP_FRESH_HORIZON. Failures are logged and retried
        next interval; reviews still surface them through write_session_copy."""
        while True:
            try:
                self._ensure_fresh(KEEP_FRESH_HORIZON.total_seconds())
            except ProviderUnavailable as exc:
                log.warning("codex auth %s: keep-fresh failed: %s", self._account, exc)
            if stop.wait(KEEP_FRESH_INTERVAL.total_seconds()):
                return

    def _ensure_fresh(self, valid_for_seconds: float) -> dict[str, Any]:
        with self._lock:
            auth = self._load()
            if self._expires_within(auth, valid_for_seconds):
                auth = self._refresh(auth)
                log.info("codex auth %s: refreshed the login", self._account)
            return auth

    def _load(self) -> dict[str, Any]:
        if not self._auth_file.exists():
            raise ProviderUnavailable(
                self._account, "auth", f"{self._auth_file} missing; {_RELOGIN}"
            )
        try:
            auth = json.loads(self._auth_file.read_text())
            tokens = auth["tokens"]
            if not tokens["access_token"] or not tokens["refresh_token"]:
                raise ValueError("empty token")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ProviderUnavailable(
                self._account,
                "auth",
                f"{self._auth_file} holds no ChatGPT login ({exc}); {_RELOGIN}",
            ) from exc
        return dict(auth)

    @staticmethod
    def _expires_within(auth: dict[str, Any], seconds: float) -> bool:
        exp = _jwt_expiry(str(auth["tokens"]["access_token"]))
        if exp is not None:
            return exp <= time.time() + seconds
        last = auth.get("last_refresh")
        if not last:
            return True
        try:
            refreshed = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        except ValueError:
            return True
        due = refreshed + TOKEN_REFRESH_INTERVAL
        return due.timestamp() <= time.time() + seconds

    def _refresh(self, auth: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._http.post(
                REFRESH_TOKEN_URL,
                json={
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": auth["tokens"]["refresh_token"],
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                self._account, "auth", f"token refresh failed: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise ProviderUnavailable(
                self._account,
                "auth",
                f"token refresh rejected (HTTP {resp.status_code}): {resp.text[:300]}; "
                f"if it says the refresh token was reused or expired, {_RELOGIN}",
            )
        try:
            fresh = resp.json()
            if not isinstance(fresh, dict) or not fresh.get("access_token"):
                raise ValueError(f"no access_token in {str(fresh)[:200]}")
        except ValueError as exc:
            raise ProviderUnavailable(
                self._account, "auth", f"token refresh answered with no token: {exc}"
            ) from exc
        tokens = dict(auth["tokens"])
        for key in ("id_token", "access_token", "refresh_token"):
            if fresh.get(key):
                tokens[key] = fresh[key]
        updated = {
            **auth,
            "tokens": tokens,
            "last_refresh": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        self._replace_auth_file(updated)
        return updated

    def _replace_auth_file(self, auth: dict[str, Any]) -> None:
        """Atomic, and keeping the file's owner and mode: the operator uid owns it
        and the codex containers that read it run as that uid."""
        st = self._auth_file.stat()
        tmp = self._auth_file.with_name(f".auth.json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(auth, indent=2))
        os.chmod(tmp, st.st_mode & 0o777)
        if os.geteuid() == 0:
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, self._auth_file)


def _jwt_expiry(token: str) -> float | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return float(exp) if isinstance(exp, int | float) else None
