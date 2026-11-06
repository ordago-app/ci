import logging
from pathlib import Path

import pytest
from src.config import ReviewConfig
from src.github_client import PullRequest
from src.job_store import JobStatus, ReviewJobStore
from src.poller import CiGreenCache, ReviewPoller


class FakeGitHub:
    def __init__(self, prs: list[PullRequest]) -> None:
        self.prs = prs

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        return self.prs


def config(
    tmp_path: Path,
    *,
    review_drafts: bool = False,
    review_dependency_bots: bool = False,
    review_mode: str = "all",
) -> ReviewConfig:
    path = tmp_path / "agent-review.yml"
    path.write_text(
        f"""
        repos:
          homelab:
            repo: alvaro/homelab
            providers: [codex]
            enabled: true
            review_drafts: {str(review_drafts).lower()}
            review_dependency_bots: {str(review_dependency_bots).lower()}
            run_ci_first: true
            review_mode: {review_mode}
            tool_profile: reviewer
        tool_profiles:
          reviewer:
            github_role: reviewer
            mcps: {{}}
            allow_shell: true
            allow_network: true
            allow_repo_write: true
            allow_git_push: false
            allow_docker: false
            max_runtime_minutes: 30
        """
    )
    return ReviewConfig.load(path)


def pr(
    *,
    draft: bool = False,
    author: str = "alice",
    sha: str = "head",
    labels: list[str] | None = None,
    number: int = 9,
) -> PullRequest:
    return PullRequest(
        number=number,
        title="Title",
        body="Body",
        draft=draft,
        state="open",
        author=author,
        base_ref="main",
        base_sha="base",
        head_ref="feature",
        head_sha=sha,
        labels=labels or [],
    )


def test_enqueues_new_open_pr_head_sha(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    poller = ReviewPoller(config(tmp_path), store, FakeGitHub([pr()]), reviewer_bot="reviewer[bot]")
    created = poller.poll_once()
    assert len(created) == 1
    assert store.list_by_status(JobStatus.QUEUED)[0].head_sha == "head"


def test_skips_draft_when_policy_disables_drafts(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    poller = ReviewPoller(
        config(tmp_path), store, FakeGitHub([pr(draft=True)]), reviewer_bot="reviewer[bot]"
    )
    assert poller.poll_once() == []


def test_skips_reviewer_bot_author(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    poller = ReviewPoller(
        config(tmp_path),
        store,
        FakeGitHub([pr(author="reviewer[bot]")]),
        reviewer_bot="reviewer[bot]",
    )
    assert poller.poll_once() == []


def test_new_sha_gets_second_job(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    gh = FakeGitHub([pr(sha="one")])
    poller = ReviewPoller(config(tmp_path), store, gh, reviewer_bot="reviewer[bot]")
    poller.poll_once()
    gh.prs = [pr(sha="two")]
    poller.poll_once()
    assert [job.head_sha for job in store.list_by_status(JobStatus.QUEUED)] == ["one", "two"]


def test_skips_bot_authored_pr_by_default(tmp_path: Path) -> None:
    # Dependency bumps are reviewed only where the repo opts in.
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    poller = ReviewPoller(
        config(tmp_path),
        store,
        FakeGitHub([pr(author="dependabot[bot]")]),
        reviewer_bot="reviewer[bot]",
    )
    assert poller.poll_once() == []


def test_reviews_dependency_bot_prs_when_opted_in(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    poller = ReviewPoller(
        config(tmp_path, review_dependency_bots=True),
        store,
        FakeGitHub(
            [
                pr(number=1, sha="one", author="dependabot[bot]"),
                pr(number=2, sha="two", author="renovate[bot]"),
            ]
        ),
        reviewer_bot="reviewer[bot]",
    )
    assert sorted(job.pr_number for job in poller.poll_once()) == [1, 2]


def test_opting_in_to_dependency_bots_still_skips_other_bots(tmp_path: Path) -> None:
    # An agent's bot identity opens labelled PRs too; the switch must not let
    # them into the queue, where they would spend the shared review budget.
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    poller = ReviewPoller(
        config(tmp_path, review_dependency_bots=True),
        store,
        FakeGitHub([pr(author="ordago-dev[bot]")]),
        reviewer_bot="reviewer[bot]",
    )
    assert poller.poll_once() == []


def test_labeled_mode_only_reviews_labelled_prs(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    gh = FakeGitHub(
        prs=[
            pr(number=1, labels=["ai-review"]),
            pr(number=2, labels=["wip"]),
        ]
    )
    created = ReviewPoller(
        config(tmp_path, review_mode="labeled"), store, gh, reviewer_bot="reviewer[bot]"
    ).poll_once()
    assert [job.pr_number for job in created] == [1]


class FakeChecks:
    def __init__(self, green: set[str] | None = None, error: Exception | None = None) -> None:
        self.green = green or set()
        self.error = error
        self.calls: list[str] = []

    def head_ci_green(self, repo: str, head_sha: str) -> bool:
        self.calls.append(head_sha)
        if self.error is not None:
            raise self.error
        return head_sha in self.green


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_ci_answer_is_cached_per_head_for_the_ttl() -> None:
    checks = FakeChecks(green={"a"})
    clock = Clock()
    cache = CiGreenCache(checks, ttl_seconds=120, clock=clock)

    assert cache.is_green("r", "a") and cache.is_green("r", "a")
    assert not cache.is_green("r", "b")
    assert checks.calls == ["a", "b"]

    clock.now += 121
    checks.green.add("b")
    assert cache.is_green("r", "b"), "re-read once the TTL has passed"
    assert checks.calls == ["a", "b", "b"]


def test_unreadable_checks_read_as_not_green_and_warn_once_per_process(
    caplog: pytest.LogCaptureFixture,
) -> None:
    checks = FakeChecks(error=RuntimeError("HTTP 403"))
    cache = CiGreenCache(checks, ttl_seconds=120, clock=Clock())
    with caplog.at_level(logging.WARNING, logger="src.poller"):
        assert not cache.is_green("r", "a")
        assert not cache.is_green("r", "b")
        assert not cache.is_green("r", "a")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "PR age" in warnings[0].getMessage()
    assert checks.calls == ["a", "b"], "a failed read is cached too, not retried every poll"


def test_poll_records_which_waiting_heads_are_green(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    checks = FakeChecks(green={"g"})
    gh = FakeGitHub([pr(number=1, sha="g"), pr(number=2, sha="r")])
    poller = ReviewPoller(
        config(tmp_path), store, gh, reviewer_bot="reviewer[bot]", ci=CiGreenCache(checks)
    )

    jobs = {job.head_sha: job for job in poller.poll_once()}

    assert jobs["g"].ci_green and not jobs["r"].ci_green
    assert store.find("alvaro-francisco-gil/homelab", 1, "g").ci_green  # type: ignore[union-attr]


def test_poll_reads_no_ci_for_a_head_already_reviewed(tmp_path: Path) -> None:
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    done = store.enqueue("alvaro-francisco-gil/homelab", "homelab", "codex", 9, "head", "base")
    store.claim(done.id, max_attempts=3)
    store.mark_posted(done.id, verdict="APPROVE")
    checks = FakeChecks(green={"head"})
    poller = ReviewPoller(
        config(tmp_path),
        store,
        FakeGitHub([pr()]),
        reviewer_bot="reviewer[bot]",
        ci=CiGreenCache(checks),
    )
    poller.poll_once()
    assert checks.calls == []
