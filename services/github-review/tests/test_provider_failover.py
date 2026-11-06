"""Failover behaviour, driven off the payloads that actually took the lab down.

The strings in QUOTA_* and MODELS_SKEW are copied verbatim from
`review_jobs.last_error` on powerserver (jobs 15625 and the 09-02 run). They are
the point of these tests: a classifier written against invented wording is a
classifier that fires on nothing real.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from src.config import ReviewConfig
from src.job_store import JobStatus, ReviewJobStore
from src.provider import (
    ProviderUnavailable,
    QuotaReading,
    ReviewLimitExceeded,
    ReviewResult,
    SessionRef,
    classify_unavailable,
)
from src.worker import ProviderOutage, ReviewWorker

from tests.helpers import tick_and_drain, write_config

from .test_worker_flow import FakeGitHub, FakeWorktrees

# job 15625, alvaro-francisco-gil/homelab#202, 2026-09-01 23:50
QUOTA_HOURLY = (
    '{"type":"error","message":"You\'ve hit your usage limit. Upgrade to Pro '
    "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
    'to purchase more credits or try again at 1:43 AM."}\n'
    '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit."}}'
)
# 2026-09-02 19:21 — same shape, but the reset is five days out, so no amount of
# retrying rides this one out. That is what makes failover load-bearing.
QUOTA_WEEKLY = (
    '{"type":"error","message":"You\'ve hit your usage limit. Upgrade to Pro '
    '... or try again at Sep 7th, 2026 3:31 AM."}'
)
# homelab#209@2b483fb, 2026-09-02 21:58 — the SECOND account going down, and
# the payload that proved the table was written for one vendor. It matched
# nothing, so claude-work raised a plain failure and the chain never reached
# claude-personal: a fallback that did not fall.
CLAUDE_SPEND_LIMIT = (
    "You've hit your org's monthly spend limit · ask your admin to raise it at "
    "claude.ai/settings/usage?from=cc_cli_limit_message · your session limit "
    "resets 12:50am (UTC)"
)
API_CREDITS_SPENT = (
    "Your credit balance is too low to access the Anthropic API. "
    "Please go to Plans & Billing to upgrade or purchase credits."
)
MODELS_SKEW = (
    "failed to refresh available models: unknown variant `max`, "
    "expected one of none|minimal|low|medium|high|xhigh"
)
# jobs 20300/20366/20541/20556, 2026-09-07, ordago-apps#901/#904/#906 and
# homelab#214. The pinned `gpt-5.5` 404s intermittently for this account: these
# four failed while jobs 20291/20355/20479/20540/20557 posted fine on the same
# model the same day, so it is a flaky backend rather than a retired model.
# It is the 09-01 skew from the other side — last time the CLI could not parse
# the models API, this time the API rejects the model the CLI was told to use.
# Reaching the model is strictly before reading the diff, so it is `tooling`,
# not a job failure, and classifying it is what stops an intermittent provider
# from failing whichever PRs it lands on.
MODEL_NOT_FOUND = (
    '{"type":"error","message":"Reconnecting... 5/5 (unexpected status 404 Not Found: '
    "The model `gpt-5.5` does not exist or you do not have access to it., url: "
    "https://chatgpt.com/backend-api/codex/responses, cf-ray: a3775633982ec9f9-MAD, "
    'request id: d272f815-e569-4fa6-8fe9-88db4dad8613)"}\n'
    '{"type":"turn.failed","error":{"message":"unexpected status 404 Not Found: '
    "The model `gpt-5.5` does not exist or you do not have access to it."
    '"}}'
)
# jobs 16666/16667, ordago-app/ordago-apps#872/#873, 2026-09-02. The THIRD
# wording of a spent Claude subscription, and it drops every noun the table was
# keyed on: no "usage limit", no "spend limit", no "session limit", and no
# cc_cli_limit_message URL to fall back to. Just "your limit".
CLAUDE_BARE_LIMIT = "You've hit your limit \u00b7 resets 2:50am (UTC)"


@pytest.mark.parametrize(
    ("sample", "reason"),
    [
        (QUOTA_HOURLY, "exhausted"),
        (QUOTA_WEEKLY, "exhausted"),
        (CLAUDE_SPEND_LIMIT, "exhausted"),
        (MODELS_SKEW, "tooling"),
        (MODEL_NOT_FOUND, "tooling"),
        (CLAUDE_BARE_LIMIT, "exhausted"),
        ("Codex auth is not onboarded; run `make codex-onboard`", "auth"),
        # Claude Code's own strings for an empty API balance (CLI 2.1.317): the
        # API's sentence, and the shorter one the CLI renders.
        (API_CREDITS_SPENT, "exhausted"),
        ("Credit balance too low · Add funds: https://platform.claude.com/settings", "exhausted"),
    ],
)
def test_classifies_real_outage_payloads(sample: str, reason: str) -> None:
    assert classify_unavailable(sample) == reason


def test_does_not_classify_an_ordinary_job_failure() -> None:
    """A failure that is about the diff must never fail over.

    Burning the spare subscription on a problem the spare shares is worse than
    failing: it turns one red check into three, slowly.
    """
    assert classify_unavailable("AssertionError: expected 3, got 4") is None
    assert classify_unavailable("codex review timed out") is None


class ScriptedProvider:
    """A provider that either answers or reports itself unavailable."""

    def __init__(
        self, name: str, *, unavailable: str | None = None, body: str = "No findings."
    ) -> None:
        self.name = name
        self.unavailable = unavailable
        self.body = body
        self.runs = 0
        self.cleaned = 0

    def start_review_session(self, job, worktree, timeout_seconds) -> SessionRef:
        return SessionRef(id=str(job.id), container=f"c-{self.name}", worktree=worktree)

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        self.runs += 1
        if self.unavailable:
            raise ProviderUnavailable(self.name, self.unavailable, "spent")
        return ReviewResult(body=self.body, event="APPROVE")

    def cleanup(self, session: SessionRef) -> None:
        self.cleaned += 1


def _config(tmp_path: Path, chain: str) -> ReviewConfig:
    return write_config(tmp_path, {"homelab": {"providers": yaml.safe_load(chain)}})


def _worker(tmp_path: Path, providers: dict, chain: str, gh: FakeGitHub) -> ReviewWorker:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    return ReviewWorker(
        config=_config(tmp_path, chain),
        store=store,
        github=gh,
        worktrees=FakeWorktrees(tmp_path / "wt"),
        providers=providers,
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
    )


def test_exhausted_primary_falls_over_to_the_first_claude_account(tmp_path: Path) -> None:
    gh = FakeGitHub()
    codex = ScriptedProvider("codex", unavailable="exhausted")
    work = ScriptedProvider("claude-work")
    personal = ScriptedProvider("claude-personal")
    worker = _worker(
        tmp_path,
        {"codex": codex, "claude-work": work, "claude-personal": personal},
        "[codex, claude-work, claude-personal]",
        gh,
    )

    tick_and_drain(worker)

    assert work.runs == 1, "the review must actually happen on the second provider"
    assert personal.runs == 0, "the chain stops at the first provider that can run"
    posted = gh.posted[0]
    assert posted[3] == "APPROVE"
    assert worker.store.list_by_status(JobStatus.POSTED)[0].provider == "claude-work"


def test_the_posted_review_says_who_wrote_it_and_what_is_broken(tmp_path: Path) -> None:
    """The disclosure banner IS the notification.

    Without it a fallback review is indistinguishable from a normal one, and the
    outage leaves no trace on the surface every reviewer and agent actually
    reads. That is how the reviewer stayed broken for seven weeks in June.
    """
    gh = FakeGitHub()
    worker = _worker(
        tmp_path,
        {
            "codex": ScriptedProvider("codex", unavailable="exhausted"),
            "claude-work": ScriptedProvider("claude-work", body="Looks good."),
        },
        "[codex, claude-work]",
        gh,
    )

    tick_and_drain(worker)

    body = gh.posted[0][2]
    assert "claude-work" in body
    assert "codex" in body and "exhausted" in body
    assert "Looks good." in body, "the actual review must survive the banner"


def test_a_normal_review_is_not_annotated(tmp_path: Path) -> None:
    gh = FakeGitHub()
    worker = _worker(tmp_path, {"codex": ScriptedProvider("codex")}, "[codex]", gh)

    tick_and_drain(worker)

    assert gh.posted[0][2] == "No findings."


def test_an_ordinary_error_does_not_advance_the_chain(tmp_path: Path) -> None:
    class Boom(ScriptedProvider):
        def run_review(self, session, prompt, timeout_seconds):
            self.runs += 1
            raise RuntimeError("container died")

    gh = FakeGitHub()
    codex = Boom("codex")
    work = ScriptedProvider("claude-work")
    worker = _worker(tmp_path, {"codex": codex, "claude-work": work}, "[codex, claude-work]", gh)

    tick_and_drain(worker)

    assert work.runs == 0, "a job-level failure must not spend the fallback subscription"
    assert gh.posted == []
    assert worker.store.list_by_status(JobStatus.FAILED)


def test_a_review_that_hits_its_cap_is_given_up_not_failed_over(tmp_path: Path) -> None:
    """The next account would spend as much again and stop at the same cap, and
    so would a retry: the head is failed for good, its run counted, and the PR
    told why no review is coming."""

    class Capped(ScriptedProvider):
        def run_review(self, session, prompt, timeout_seconds):
            self.runs += 1
            raise ReviewLimitExceeded(self.name, "Reached maximum budget ($3)")

    gh = FakeGitHub()
    api = Capped("claude-api")
    spare = ScriptedProvider("claude-work")
    worker = _worker(
        tmp_path, {"claude-api": api, "claude-work": spare}, "[claude-api, claude-work]", gh
    )

    tick_and_drain(worker)
    tick_and_drain(worker)

    assert (api.runs, spare.runs) == (1, 0)
    assert gh.posted == []
    failed = worker.store.list_by_status(JobStatus.FAILED)
    assert len(failed) == 1 and failed[0].attempts >= worker.max_attempts
    assert worker.store.runs_for(failed[0].repo, failed[0].pr_number) == 1
    assert len(gh.comments) == 1 and "maximum budget" in gh.comments[0][2]
    assert "cost cap" in gh.comments[0][2]


def test_both_claude_accounts_are_tried_before_giving_up(tmp_path: Path) -> None:
    gh = FakeGitHub()
    personal = ScriptedProvider("claude-personal")
    worker = _worker(
        tmp_path,
        {
            "codex": ScriptedProvider("codex", unavailable="exhausted"),
            "claude-work": ScriptedProvider("claude-work", unavailable="exhausted"),
            "claude-personal": personal,
        },
        "[codex, claude-work, claude-personal]",
        gh,
    )

    tick_and_drain(worker)

    assert personal.runs == 1
    assert gh.posted[0][2].count("exhausted") >= 1
    assert "claude-work" in gh.posted[0][2] and "codex" in gh.posted[0][2]


def test_whole_chain_down_names_every_provider_not_just_the_last(tmp_path: Path) -> None:
    """An operator seeing only the last failure cannot tell one dead account
    from three, which is the difference between "wait" and "go fix it now"."""
    gh = FakeGitHub()
    worker = _worker(
        tmp_path,
        {
            "codex": ScriptedProvider("codex", unavailable="exhausted"),
            "claude-work": ScriptedProvider("claude-work", unavailable="auth"),
        },
        "[codex, claude-work]",
        gh,
    )

    tick_and_drain(worker)

    failed = worker.store.list_by_status(JobStatus.FAILED)[0]
    assert "codex" in failed.last_error and "claude-work" in failed.last_error
    assert "exhausted" in failed.last_error and "auth" in failed.last_error
    assert gh.posted == []


def test_degraded_providers_are_reported_and_cleared(tmp_path: Path) -> None:
    gh = FakeGitHub()
    codex = ScriptedProvider("codex", unavailable="exhausted")
    worker = _worker(
        tmp_path,
        {"codex": codex, "claude-work": ScriptedProvider("claude-work")},
        "[codex, claude-work]",
        gh,
    )

    tick_and_drain(worker)
    assert [o["provider"] for o in worker.degraded_providers()] == ["codex"]

    # Codex recovers; the next review must stop reporting it as down.
    codex.unavailable = None
    # PR 2, same head the FakeGitHub reports — a job whose head has moved is
    # skipped without ever reaching a provider, which would clear nothing.
    worker.store.enqueue("alvaro-francisco-gil/homelab", "homelab", "codex", 2, "head", "base")
    tick_and_drain(worker)
    assert worker.degraded_providers() == []


def test_every_session_is_cleaned_up_even_the_unavailable_ones(tmp_path: Path) -> None:
    """A leaked container per outage exhausts the host, and an outage is
    exactly when many reviews fail in a row."""
    gh = FakeGitHub()
    codex = ScriptedProvider("codex", unavailable="exhausted")
    work = ScriptedProvider("claude-work")
    worker = _worker(tmp_path, {"codex": codex, "claude-work": work}, "[codex, claude-work]", gh)

    tick_and_drain(worker)

    assert codex.cleaned == 1
    assert work.cleaned == 1


def test_the_second_account_going_down_still_advances_the_chain(tmp_path: Path) -> None:
    """The failure that exposed the one-vendor table.

    Both upstreams word exhaustion differently, and an unrecognised wording is
    not a soft failure — it stops the chain dead at the link before the last
    one, which is the same outcome as having no fallback at all.
    """
    gh = FakeGitHub()
    personal = ScriptedProvider("claude-personal")
    worker = _worker(
        tmp_path,
        {
            "codex": ScriptedProvider("codex", unavailable="exhausted"),
            "claude-work": ScriptedProvider("claude-work", unavailable="exhausted"),
            "claude-personal": personal,
        },
        "[codex, claude-work, claude-personal]",
        gh,
    )

    tick_and_drain(worker)

    assert personal.runs == 1, "the last account must be reached when both above it are spent"
    assert gh.posted, "a review must still be posted"


# ── reviewer quota rows ───────────────────────────────────────────────────────


def _quota(account: str, utilization: float | None):
    return QuotaReading(
        account=account,
        status="allowed_warning" if utilization else "allowed",
        limit_type="overage" if utilization else "five_hour",
        utilization=utilization,
        resets_at=1790812800.0,
        observed_at=1788516000.0,
    )


def _quota_worker(providers: dict) -> ReviewWorker:
    return ReviewWorker(
        config=MagicMock(),
        store=MagicMock(),
        github=MagicMock(),
        worktrees=MagicMock(),
        providers=providers,
        projects_root=Path("/tmp"),
        reviewer_bot="bot",
    )


def test_quota_rows_follow_the_chain_not_how_spent_each_account_is(tmp_path: Path) -> None:
    """The order IS the failover order. Sorting by utilization would hide which
    account answers the next review — the question the card exists for. So would
    listing them as main.py registers them, once the config swaps two accounts."""
    spent = MagicMock()
    spent.last_quota = _quota("claude-personal", 92.0)
    fresh = MagicMock()
    fresh.last_quota = _quota("claude-work", 3.0)
    worker = _worker(
        tmp_path,
        {"codex": MagicMock(), "claude-work": fresh, "claude-personal": spent},
        "[codex, claude-personal, claude-work]",
        FakeGitHub(),
    )

    rows = worker.reviewer_quota()

    assert [r["provider"] for r in rows] == ["codex", "claude-personal", "claude-work"]


def test_a_registered_provider_no_chain_names_still_gets_a_row(tmp_path: Path) -> None:
    worker = _worker(
        tmp_path,
        {"codex": MagicMock(), "claude-work": MagicMock(), "claude-personal": MagicMock()},
        "[claude-personal]",
        FakeGitHub(),
    )

    rows = worker.reviewer_quota()

    assert [r["provider"] for r in rows] == ["claude-personal", "codex", "claude-work"]


def test_codex_reports_no_quota_rather_than_a_fabricated_one() -> None:
    """codex-cli emits token counts and nothing about rate limits, so its row
    must carry a null reading. A zero here would render a provider that ran out
    last night as the one with the most headroom."""
    codex = MagicMock(spec=[])  # no last_quota attribute at all
    worker = _quota_worker({"codex": codex})

    assert worker.reviewer_quota() == [
        {
            "provider": "codex",
            "reports_quota": False,
            "quota": None,
            "outage": None,
            "last_ok": None,
        }
    ]


def test_a_claude_account_that_has_not_run_yet_reports_no_quota() -> None:
    never_ran = MagicMock()
    never_ran.last_quota = None
    worker = _quota_worker({"claude-work": never_ran})

    assert worker.reviewer_quota()[0]["quota"] is None


def test_the_codex_row_carries_its_outage_with_the_reset_time_intact() -> None:
    """The exhaustion message is the ONLY quota signal codex ever gives, and the
    reset time inside it ('try again at 1:29 PM') is the single most actionable
    field in the outage — so the row must not summarise it away."""
    raw = (
        "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
        "visit https://chatgpt.com/codex/settings/usage to purchase more credits or "
        "try again at 1:29 PM."
    )
    worker = _quota_worker({"codex": MagicMock(spec=[])})
    worker._outages["codex"] = ProviderOutage(
        provider="codex", reason="exhausted", detail=raw, since=1788516000.0
    )

    row = worker.reviewer_quota()[0]

    assert row["outage"]["reason"] == "exhausted"
    assert "try again at 1:29 PM" in row["outage"]["detail"]


def test_a_recovered_provider_is_distinguishable_from_one_that_never_ran() -> None:
    """An outage that clears leaves nothing behind, so without last_ok a codex
    that recovered five minutes ago renders identically to a codex nobody has
    ever exercised."""
    worker = _quota_worker({"codex": MagicMock(spec=[])})
    assert worker.reviewer_quota()[0]["last_ok"] is None

    worker._last_ok["codex"] = 1788516000.0
    row = worker.reviewer_quota()[0]

    assert row["last_ok"] == 1788516000.0
    assert row["outage"] is None


def test_quota_capability_comes_from_the_provider_not_from_having_a_reading() -> None:
    """Readings are in memory, so every Claude account has a null reading until
    its first review after a restart. Downstream inferred the capability from
    that null and told the operator the account had no quota API — true of codex
    alone. The flag is the provider's type, and it does not move."""
    never_ran = MagicMock()
    never_ran.last_quota = None
    worker = _quota_worker({"codex": MagicMock(spec=[]), "claude-work": never_ran})

    rows = {r["provider"]: r for r in worker.reviewer_quota()}

    assert rows["claude-work"]["reports_quota"] is True
    assert rows["claude-work"]["quota"] is None
    assert rows["codex"]["reports_quota"] is False
