"""Ranking subscriptions by the weekly credit each would lose unused.

The header set below is copied from a real probe of a `claude setup-token`
token on 2026-11-20: it is the only quota signal such a token can read.
"""

import threading
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from src.config import ClaudeAccount, ConfigError, QuotaRanking
from src.job_store import JobStatus, ReviewJobStore
from src.quota_rank import (
    PROBE_URL,
    QuotaLedger,
    QuotaProber,
    Window,
    expiring_per_hour,
    next_probe,
    order_chain,
    rank,
    run_probes,
    window_kind,
    windows_from_headers,
)
from src.worker import ReviewWorker

from tests.helpers import account, tick_and_drain, write_config

from .test_provider_failover import ScriptedProvider
from .test_worker_flow import FakeGitHub, FakeWorktrees

NOW = 1_795_100_000.0
HOUR = 3600.0

REAL_HEADERS = {
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
    "anthropic-ratelimit-unified-reset": "1795140000",
    "anthropic-ratelimit-unified-5h-utilization": "0.58",
    "anthropic-ratelimit-unified-7d-reset": "1795298400",
    "anthropic-ratelimit-unified-5h-reset": "1795140000",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-utilization": "0.47",
    "anthropic-ratelimit-unified-status": "allowed",
}

RANKING = {
    "accounts": ["sub-a", "sub-b"],
    "probe_at": ["07:00", "12:01", "17:02"],
    "timezone": "Europe/Madrid",
    "probe_model": "claude-haiku-test",
}


def weekly(ledger: QuotaLedger, name: str, used: float, hours_left: float) -> None:
    ledger.record(name, "seven_day", Window(used, NOW + hours_left * HOUR, NOW))


# --- reading ------------------------------------------------------------------


def test_reads_both_windows_from_a_real_probe_response() -> None:
    windows = windows_from_headers(REAL_HEADERS, NOW)

    assert windows["five_hour"] == Window(0.58, 1795140000.0, NOW)
    assert windows["seven_day"] == Window(0.47, 1795298400.0, NOW)


def test_a_window_without_its_numbers_is_left_out_not_zeroed() -> None:
    headers = {"anthropic-ratelimit-unified-7d-reset": "1795298400"}

    assert windows_from_headers(headers, NOW) == {}


def test_review_events_name_the_window_they_report() -> None:
    assert window_kind("five_hour") == "five_hour"
    assert window_kind("seven_day") == "seven_day"
    assert window_kind("weekly") == "seven_day"
    assert window_kind("overage") is None


# --- ranking ------------------------------------------------------------------


def test_the_account_losing_the_most_credit_per_hour_goes_first() -> None:
    ledger = QuotaLedger()
    weekly(ledger, "far", used=0.30, hours_left=120)  # 0.70 over 5 days
    weekly(ledger, "near", used=0.60, hours_left=30)  # 0.40 over 30 h

    assert rank(["far", "near"], ledger, NOW) == ["near", "far"]


def test_an_almost_spent_account_near_its_reset_ranks_by_what_it_has_left() -> None:
    """ "Closest to reset" alone would put this first; 6% left is less to lose."""
    ledger = QuotaLedger()
    weekly(ledger, "spent", used=0.94, hours_left=2)  # 0.06 over one window
    weekly(ledger, "fresh", used=0.10, hours_left=30)  # 0.90 over 30 h

    assert rank(["spent", "fresh"], ledger, NOW) == ["fresh", "spent"]


def test_an_hour_from_reset_counts_as_one_window_not_one_hour() -> None:
    ledger = QuotaLedger()
    weekly(ledger, "x", used=0.5, hours_left=1)

    assert expiring_per_hour(ledger.windows("x")["seven_day"], NOW) == pytest.approx(0.5 / 5)


def test_a_full_week_goes_last_but_stays_in_the_chain() -> None:
    ledger = QuotaLedger()
    weekly(ledger, "full", used=0.97, hours_left=10)
    weekly(ledger, "ok", used=0.20, hours_left=150)

    assert rank(["full", "ok"], ledger, NOW) == ["ok", "full"]


def test_a_fresh_full_five_hour_window_goes_last() -> None:
    ledger = QuotaLedger()
    weekly(ledger, "busy", used=0.40, hours_left=20)
    ledger.record("busy", "five_hour", Window(0.90, NOW + 2 * HOUR, NOW - 60))
    weekly(ledger, "idle", used=0.40, hours_left=100)

    assert rank(["busy", "idle"], ledger, NOW) == ["idle", "busy"]


def test_an_old_five_hour_reading_says_nothing_about_now() -> None:
    ledger = QuotaLedger()
    weekly(ledger, "busy", used=0.40, hours_left=20)
    ledger.record("busy", "five_hour", Window(0.90, NOW + 2 * HOUR, NOW - HOUR))
    weekly(ledger, "idle", used=0.40, hours_left=100)

    assert rank(["busy", "idle"], ledger, NOW) == ["busy", "idle"]


def test_no_readings_keeps_the_configured_order() -> None:
    assert rank(["b", "a", "c"], QuotaLedger(), NOW) == ["b", "a", "c"]


def test_unread_accounts_follow_ranked_ones_in_their_configured_order() -> None:
    ledger = QuotaLedger()
    weekly(ledger, "read", used=0.5, hours_left=50)

    assert rank(["u1", "read", "u2"], ledger, NOW) == ["read", "u1", "u2"]


def test_a_reading_past_its_reset_or_a_day_old_is_no_reading() -> None:
    ledger = QuotaLedger()
    ledger.record("reset", "seven_day", Window(0.1, NOW - 1, NOW - HOUR))
    ledger.record("stale", "seven_day", Window(0.1, NOW + 10 * HOUR, NOW - 25 * HOUR))
    weekly(ledger, "read", used=0.9, hours_left=100)

    assert rank(["reset", "stale", "read"], ledger, NOW) == ["read", "reset", "stale"]


def test_only_the_ranked_accounts_change_places_in_a_chain() -> None:
    ledger = QuotaLedger()
    weekly(ledger, "sub-a", used=0.1, hours_left=150)
    weekly(ledger, "sub-b", used=0.5, hours_left=20)
    chain = ["codex", "api", "sub-a", "sub-b", "api-last"]

    assert order_chain(chain, ["sub-a", "sub-b"], ledger, NOW) == [
        "codex",
        "api",
        "sub-b",
        "sub-a",
        "api-last",
    ]


# --- probing ------------------------------------------------------------------


def _prober(tmp_path: Path, ledger: QuotaLedger, environ: dict[str, str]) -> QuotaProber:
    config = write_config(
        tmp_path,
        {"homelab": {"providers": ["codex", "sub-a", "sub-b"]}},
        quota_ranking=RANKING,
    )
    accounts = {n: a for n, a in config.accounts.items() if isinstance(a, ClaudeAccount)}
    return QuotaProber(
        accounts=accounts,
        ledger=ledger,
        model="claude-haiku-test",
        environ=environ,
        http=httpx.Client(),
        sleep=lambda _: None,
    )


@respx.mock
def test_a_probe_records_both_windows_and_sends_the_token(tmp_path: Path) -> None:
    route = respx.post(PROBE_URL).mock(return_value=httpx.Response(200, headers=REAL_HEADERS))
    ledger = QuotaLedger()

    _prober(tmp_path, ledger, {"TOKEN_SUB_A": "tok-a", "TOKEN_SUB_B": "tok-b"}).probe_all()

    assert route.call_count == 2
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok-a"
    assert ledger.windows("sub-a")["seven_day"].utilization == 0.47
    assert ledger.windows("sub-b")["five_hour"].utilization == 0.58


@respx.mock
def test_a_full_account_still_reports_through_its_429(tmp_path: Path) -> None:
    respx.post(PROBE_URL).mock(return_value=httpx.Response(429, headers=REAL_HEADERS))
    ledger = QuotaLedger()

    _prober(tmp_path, ledger, {"TOKEN_SUB_A": "tok-a"}).probe_all()

    assert "seven_day" in ledger.windows("sub-a")


@respx.mock
def test_a_failing_probe_never_raises_and_the_next_account_is_still_read(tmp_path: Path) -> None:
    respx.post(PROBE_URL).mock(
        side_effect=[
            httpx.ConnectError("down"),
            httpx.ConnectError("down"),
            httpx.ConnectError("down"),
            httpx.Response(200, headers=REAL_HEADERS),
        ]
    )
    ledger = QuotaLedger()

    _prober(tmp_path, ledger, {"TOKEN_SUB_A": "tok-a", "TOKEN_SUB_B": "tok-b"}).probe_all()

    assert ledger.windows("sub-a") == {}
    assert "seven_day" in ledger.windows("sub-b")


@respx.mock
def test_an_account_with_no_credential_is_skipped_without_a_request(tmp_path: Path) -> None:
    route = respx.post(PROBE_URL).mock(return_value=httpx.Response(200, headers=REAL_HEADERS))

    _prober(tmp_path, QuotaLedger(), {"TOKEN_SUB_B": "tok-b"}).probe_all()

    assert route.call_count == 1


def test_the_next_probe_is_the_next_local_time_across_midnight_and_dst() -> None:
    settings = QuotaRanking.model_validate(RANKING)

    # 2026-11-20 10:00 UTC = 11:00 Madrid (CET)
    assert next_probe(settings, datetime(2026, 11, 20, 10, 0, tzinfo=UTC)).isoformat() == (
        "2026-11-20T12:01:00+01:00"
    )
    # 18:00 Madrid -> tomorrow 07:00
    assert next_probe(settings, datetime(2026, 11, 20, 17, 0, tzinfo=UTC)).isoformat() == (
        "2026-11-21T07:00:00+01:00"
    )
    # summer time: 05:30 UTC = 07:30 CEST -> 12:01 CEST
    assert next_probe(settings, datetime(2026, 7, 1, 5, 30, tzinfo=UTC)).isoformat() == (
        "2026-07-01T12:01:00+02:00"
    )


def test_the_probe_loop_stops_when_told(tmp_path: Path) -> None:
    stop = threading.Event()
    stop.set()
    prober = _prober(tmp_path, QuotaLedger(), {})

    run_probes(QuotaRanking.model_validate(RANKING), prober, stop)  # returns, no probe


# --- config -------------------------------------------------------------------


def test_only_declared_subscriptions_can_be_ranked(tmp_path: Path) -> None:
    api = {**account("sub-b"), "auth": "api_key", "max_budget_usd": 1}
    with pytest.raises(ConfigError, match="not a declared Claude subscription"):
        write_config(
            tmp_path,
            {"homelab": {"providers": ["codex", "sub-a", "sub-b"]}},
            accounts={"codex": account("codex"), "sub-a": account("sub-a"), "sub-b": api},
            quota_ranking=RANKING,
        )


@pytest.mark.parametrize(
    "override, message",
    [
        ({"timezone": "Mars/Olympus"}, "unknown timezone"),
        ({"probe_at": ["7:00"]}, "not HH:MM"),
        ({"accounts": ["sub-a"]}, "at least 2"),
    ],
)
def test_a_bad_ranking_block_stops_the_service(tmp_path: Path, override, message) -> None:
    with pytest.raises(ConfigError, match=message):
        write_config(
            tmp_path,
            {"homelab": {"providers": ["codex", "sub-a", "sub-b"]}},
            quota_ranking={**RANKING, **override},
        )


# --- the worker ---------------------------------------------------------------


def test_a_review_falls_over_to_the_subscription_losing_the_most_credit(
    tmp_path: Path,
) -> None:
    config = write_config(
        tmp_path,
        {"homelab": {"providers": ["codex", "sub-a", "sub-b"]}},
        quota_ranking=RANKING,
    )
    ledger = QuotaLedger()
    now = datetime.now().timestamp()
    ledger.record("sub-a", "seven_day", Window(0.1, now + 150 * HOUR, now))
    ledger.record("sub-b", "seven_day", Window(0.5, now + 20 * HOUR, now))
    providers = {
        "codex": ScriptedProvider("codex", unavailable="exhausted"),
        "sub-a": ScriptedProvider("sub-a"),
        "sub-b": ScriptedProvider("sub-b"),
    }
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    worker = ReviewWorker(
        config=config,
        store=store,
        github=FakeGitHub(),
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers=providers,
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
        quota_ledger=ledger,
    )

    tick_and_drain(worker)

    assert providers["sub-b"].runs == 1
    assert providers["sub-a"].runs == 0
    assert worker.store.list_by_status(JobStatus.POSTED)[0].provider == "sub-b"
    status = worker.quota_ranking()
    assert status is not None and status["order"] == ["sub-b", "sub-a"]


def test_codex_keeps_first_place_whatever_the_subscriptions_read(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        {"homelab": {"providers": ["codex", "sub-a", "sub-b"]}},
        quota_ranking=RANKING,
    )
    ledger = QuotaLedger()
    now = datetime.now().timestamp()
    ledger.record("sub-b", "seven_day", Window(0.0, now + 6 * HOUR, now))
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    worker = ReviewWorker(
        config=config,
        store=store,
        github=FakeGitHub(),
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers={},
        projects_root=tmp_path,
        reviewer_bot="reviewer[bot]",
        quota_ledger=ledger,
    )

    assert worker.review_chain(["codex", "sub-a", "sub-b"]) == ["codex", "sub-b", "sub-a"]
