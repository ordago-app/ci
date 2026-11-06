import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from src.provider import ProviderUnavailable
from src.providers import codex_auth
from src.providers.codex_auth import CLIENT_ID, REFRESH_TOKEN_URL, CodexAuthOwner

from tests.helpers import jwt, login


def _state(tmp_path: Path, auth: str) -> Path:
    state = tmp_path / "codex-state"
    state.mkdir()
    (state / "auth.json").write_text(auth)
    (state / "auth.json").chmod(0o600)
    return state


def test_a_fresh_login_is_copied_without_its_refresh_token_and_not_refreshed(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path, login())
    dest = tmp_path / "session-auth.json"
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(REFRESH_TOKEN_URL)
        CodexAuthOwner(state).write_session_copy(dest, valid_for_seconds=2400)
    assert not route.called
    copy = json.loads(dest.read_text())
    assert copy["tokens"]["refresh_token"] == ""
    shared = json.loads((state / "auth.json").read_text())
    assert copy["tokens"]["access_token"] == shared["tokens"]["access_token"]
    assert shared["tokens"]["refresh_token"] == "rt-1"
    assert dest.stat().st_mode & 0o777 == 0o400, "it still holds a live access token"


def test_a_token_that_would_expire_mid_review_is_refreshed_once_and_persisted(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path, login(expires_in=600))
    new_access = jwt(time.time() + 10 * 24 * 3600)
    with respx.mock() as mock:
        route = mock.post(REFRESH_TOKEN_URL).respond(
            200, json={"access_token": new_access, "refresh_token": "rt-2", "id_token": "id-2"}
        )
        CodexAuthOwner(state).write_session_copy(tmp_path / "a.json", valid_for_seconds=2400)
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": "rt-1"}
    shared = json.loads((state / "auth.json").read_text())
    assert shared["tokens"] == {
        "id_token": "id-2",
        "access_token": new_access,
        "refresh_token": "rt-2",
        "account_id": "acct",
    }
    assert shared["last_refresh"] != "2026-10-10T00:00:00Z"
    assert (state / "auth.json").stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / "a.json").read_text())["tokens"]["access_token"] == new_access


def test_concurrent_sessions_spend_the_refresh_token_once(tmp_path: Path) -> None:
    """The race this owner exists for: N sessions starting together on a stale
    token must produce one refresh, not N uses of a single-use token."""
    state = _state(tmp_path, login(expires_in=60))
    owner = CodexAuthOwner(state)
    seen: list[str] = []

    def refresh(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["refresh_token"])
        time.sleep(0.05)
        return httpx.Response(
            200,
            json={
                "access_token": jwt(time.time() + 864000),
                "refresh_token": f"rt-{len(seen) + 1}",
            },
        )

    with respx.mock() as mock:
        mock.post(REFRESH_TOKEN_URL).mock(side_effect=refresh)
        threads = [
            threading.Thread(target=owner.write_session_copy, args=(tmp_path / f"s{i}.json", 2400))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert seen == ["rt-1"]


def test_a_rejected_refresh_is_an_auth_outage_and_leaves_the_login_alone(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path, login(expires_in=60))
    before = (state / "auth.json").read_text()
    with respx.mock() as mock:
        mock.post(REFRESH_TOKEN_URL).respond(400, json={"error": "refresh_token_reused"})
        with pytest.raises(ProviderUnavailable) as exc:
            CodexAuthOwner(state).write_session_copy(tmp_path / "a.json", 2400)
    assert exc.value.reason == "auth"
    assert "refresh_token_reused" in exc.value.detail
    assert (state / "auth.json").read_text() == before
    assert not (tmp_path / "a.json").exists()


@pytest.mark.parametrize(
    "content", [None, "{}", '{"tokens": {"access_token": "", "refresh_token": ""}}']
)
def test_no_login_is_an_auth_outage_naming_the_fix(tmp_path: Path, content: str | None) -> None:
    state = tmp_path / "codex-state"
    state.mkdir()
    if content is not None:
        (state / "auth.json").write_text(content)
    with pytest.raises(ProviderUnavailable) as exc:
        CodexAuthOwner(state).write_session_copy(tmp_path / "a.json", 2400)
    assert exc.value.reason == "auth"
    assert "codex-onboard" in exc.value.detail


def test_a_refresh_that_times_out_is_an_auth_outage_and_changes_nothing(tmp_path: Path) -> None:
    state = _state(tmp_path, login(expires_in=60))
    before = (state / "auth.json").read_text()
    with respx.mock() as mock:
        mock.post(REFRESH_TOKEN_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(ProviderUnavailable) as exc:
            CodexAuthOwner(state).write_session_copy(tmp_path / "a.json", 2400)
    assert exc.value.reason == "auth"
    assert "slow" in exc.value.detail
    assert (state / "auth.json").read_text() == before
    assert not (tmp_path / "a.json").exists()


@pytest.mark.parametrize("answer", ["<html>maintenance</html>", "{}", "[]", '{"access_token": ""}'])
def test_a_refresh_answer_without_an_access_token_is_an_auth_outage(
    tmp_path: Path, answer: str
) -> None:
    state = _state(tmp_path, login(expires_in=60))
    before = (state / "auth.json").read_text()
    with respx.mock() as mock:
        mock.post(REFRESH_TOKEN_URL).respond(200, text=answer)
        with pytest.raises(ProviderUnavailable) as exc:
            CodexAuthOwner(state).write_session_copy(tmp_path / "a.json", 2400)
    assert exc.value.reason == "auth"
    assert (state / "auth.json").read_text() == before


NOW = 2_000_000_000.0


@pytest.mark.parametrize(
    ("expires_at", "refreshes"),
    [(NOW + 2400, True), (NOW + 2399, True), (NOW + 2401, False)],
)
def test_the_refresh_cutoff_is_the_review_s_whole_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expires_at: float, refreshes: bool
) -> None:
    monkeypatch.setattr(codex_auth.time, "time", lambda: NOW)
    auth = json.loads(login())
    auth["tokens"]["access_token"] = jwt(expires_at)
    state = _state(tmp_path, json.dumps(auth))
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(REFRESH_TOKEN_URL).respond(200, json={"access_token": jwt(NOW + 864000)})
        CodexAuthOwner(state).write_session_copy(tmp_path / "a.json", valid_for_seconds=2400)
    assert route.called is refreshes


@pytest.mark.parametrize(
    ("last_refresh", "refreshes"),
    [
        (None, True),
        ("not a date", True),
        # Due at last_refresh + 8 days; the cutoff is NOW + 2400 s.
        (datetime.fromtimestamp(NOW + 2400, UTC) - timedelta(days=8), True),
        (datetime.fromtimestamp(NOW + 2401, UTC) - timedelta(days=8), False),
    ],
)
def test_without_a_readable_expiry_last_refresh_decides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    last_refresh: datetime | str | None,
    refreshes: bool,
) -> None:
    monkeypatch.setattr(codex_auth.time, "time", lambda: NOW)
    auth = json.loads(login())
    auth["tokens"]["access_token"] = "opaque"
    if last_refresh is None:
        del auth["last_refresh"]
    elif isinstance(last_refresh, datetime):
        auth["last_refresh"] = last_refresh.isoformat().replace("+00:00", "Z")
    else:
        auth["last_refresh"] = last_refresh
    state = _state(tmp_path, json.dumps(auth))
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(REFRESH_TOKEN_URL).respond(200, json={"access_token": "opaque-2"})
        CodexAuthOwner(state).write_session_copy(tmp_path / "a.json", valid_for_seconds=2400)
    assert route.called is refreshes


def _keep_fresh_once(state: Path) -> None:
    stop = threading.Event()
    stop.set()  # one pass, then the wait returns at once
    CodexAuthOwner(state).keep_fresh(stop)


def test_keep_fresh_refreshes_a_login_inside_the_horizon_with_no_review_running(
    tmp_path: Path,
) -> None:
    # The host's other Codex consumers copy this file and cannot refresh, so
    # the owner keeps it valid KEEP_FRESH_HORIZON ahead on its own.
    state = _state(tmp_path, login(expires_in=3600))
    new_access = jwt(time.time() + 10 * 24 * 3600)
    with respx.mock() as mock:
        mock.post(REFRESH_TOKEN_URL).respond(
            200, json={"access_token": new_access, "refresh_token": "rt-2"}
        )
        _keep_fresh_once(state)
    shared = json.loads((state / "auth.json").read_text())
    assert shared["tokens"]["access_token"] == new_access
    assert shared["tokens"]["refresh_token"] == "rt-2"


def test_keep_fresh_leaves_a_login_valid_past_the_horizon_alone(tmp_path: Path) -> None:
    state = _state(tmp_path, login(expires_in=5 * 3600))
    before = (state / "auth.json").read_text()
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(REFRESH_TOKEN_URL)
        _keep_fresh_once(state)
    assert not route.called
    assert (state / "auth.json").read_text() == before


def test_keep_fresh_logs_a_failed_refresh_and_keeps_running(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = _state(tmp_path, login(expires_in=60))
    with respx.mock() as mock:
        mock.post(REFRESH_TOKEN_URL).respond(400, json={"error": "refresh_token_reused"})
        _keep_fresh_once(state)
    assert "keep-fresh failed" in caplog.text
    assert "refresh_token_reused" in caplog.text
