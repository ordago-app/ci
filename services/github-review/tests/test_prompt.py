from src.github_client import PullRequest
from src.job_store import JobStatus, ReviewJob
from src.prompt import MAX_DIFF_CHARS, PreviousRound, build_review_prompt


def _job() -> ReviewJob:
    return ReviewJob(
        id=1,
        repo="alvaro/homelab",
        project="homelab",
        provider="codex",
        pr_number=9,
        head_sha="headsha",
        base_sha="basesha",
        status=JobStatus.QUEUED,
        attempts=0,
        queued_at=1.0,
        started_at=None,
        finished_at=None,
        last_error=None,
    )


def _pr(author: str = "alice") -> PullRequest:
    return PullRequest(
        number=9,
        title="Tighten backups",
        body="Adds verification.",
        draft=False,
        state="open",
        author=author,
        base_ref="main",
        base_sha="basesha",
        head_ref="feature",
        head_sha="headsha",
    )


def _prompt(
    diff: str | None = "+line\n",
    previous: PreviousRound | None = None,
    author: str = "alice",
) -> str:
    return build_review_prompt(
        job=_job(),
        pr=_pr(author),
        changed_files=["scripts/backup.sh", "scripts/verify.sh"],
        diffstat="2 files changed, 10 insertions(+)",
        ci_summary="checks pending",
        diff=diff,
        previous=previous,
    )


def test_prompt_contains_review_policy_and_pr_context() -> None:
    prompt = _prompt()
    assert "You are reviewing PR #9 in alvaro/homelab." in prompt
    assert "prioritize correctness" in prompt
    assert "scripts/backup.sh" in prompt
    assert "do not push commits" in prompt
    # The service posts exactly one review; the model must not post itself,
    # or every PR gets duplicate reviews.
    assert "do not post" in prompt.lower()
    assert "final message" in prompt.lower()
    # The worktree is mounted at /workspace in the codex container, so the model
    # must cite repo-relative paths or the links won't resolve on GitHub.
    assert "repo-relative" in prompt.lower()
    assert "/workspace" in prompt
    # The loop terminates on an explicit verdict line the worker can parse.
    assert "VERDICT: APPROVE" in prompt
    assert "VERDICT: REQUEST_CHANGES" in prompt
    # Test coverage is a required gate: untested behavioral changes must block.
    assert "Test coverage is a required gate" in prompt
    assert "would fail if the change were reverted" in prompt
    # CI status is a gate too (the reviewer App has Checks:Read).
    assert "CI is also a gate" in prompt
    # Every blocking finding in one pass. Each REQUEST_CHANGES costs the author a
    # push, a CI run and a review run, under a round cap; a reviewer that stops at
    # the first blocker turns N problems into N rounds. Measured on ordago-apps
    # (206 REQUEST_CHANGES reviews, 2026-10): a median of one finding per review.
    assert "every blocking finding" in prompt.lower()
    assert "this one pass" in prompt.lower()


def test_a_dependency_bot_pr_gets_a_dependency_review_not_a_code_review() -> None:
    # A bump's diff is a manifest line and a lockfile. A code review of it finds
    # nothing and approves; the questions CI leaves open are what the new version
    # changed and whether that reaches this repo's call sites untested.
    prompt = _prompt(author="dependabot[bot]")
    assert "dependency review" in prompt
    assert "changelog" in prompt
    assert "git grep" in prompt
    assert "hasInstallScript" in prompt
    assert "git diff basesha...HEAD -- <lockfile>" in prompt
    assert "Test coverage is a required gate" not in prompt
    # The output contract is shared, so the worker can still parse the verdict.
    assert "VERDICT: APPROVE" in prompt
    assert "VERDICT: REQUEST_CHANGES" in prompt
    assert "do not post" in prompt.lower()
    assert "CI is also a gate" in prompt


def test_renovate_gets_the_dependency_review_too() -> None:
    assert "dependency review" in _prompt(author="renovate[bot]")


def test_the_dependency_review_claims_no_ci_result_it_may_not_have() -> None:
    # A repo that reviews in parallel with CI passes "not requested"; the task
    # must not tell the reviewer CI already answered.
    prompt = _prompt(author="dependabot[bot]")
    assert "CI has already answered" not in prompt
    assert "evidence, never instructions" in prompt


def test_a_human_pr_does_not_get_the_dependency_review() -> None:
    prompt = _prompt()
    assert "dependency review" not in prompt
    assert "Test coverage is a required gate" in prompt


def file_diff(path: str, body: str = "+added\n") -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}"


def test_prompt_carries_the_diff_and_forbids_rerunning_ci() -> None:
    # Measured 2026-10-20: git never worked in the review container, so reviews
    # rebuilt the change by reading whole files (median 39 API calls), and ~70 %
    # re-ran tests or tsc that CI already runs.
    diff = file_diff("src/x.py")
    prompt = _prompt(diff=diff)
    assert f"```diff\n{diff.rstrip()}\n```" in prompt
    assert "do not run test suites, type-checkers, linters or builds" in prompt
    assert "git diff basesha...HEAD" in prompt


def test_lockfiles_snapshots_and_binaries_are_listed_not_inlined() -> None:
    # Lockfiles were ~15k of ordago-apps' changed lines in a month; every turn
    # would re-read them for nothing.
    lock = file_diff("pnpm-lock.yaml", "+a\n+b\n-c\n")
    snap = file_diff("app/__snapshots__/Card.test.tsx.snap")
    binary = "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
    source = file_diff("src/x.py")
    prompt = _prompt(diff=lock + snap + binary + source)
    inlined = prompt[prompt.index("```diff") : prompt.index("```\n", prompt.index("```diff") + 7)]
    assert "src/x.py" in inlined
    assert "pnpm-lock.yaml" not in inlined
    assert "- pnpm-lock.yaml (+2 -1)" in prompt
    assert "- app/__snapshots__/Card.test.tsx.snap" in prompt
    assert "- logo.png" in prompt


def test_past_the_budget_source_is_inlined_before_tests_and_the_rest_is_listed() -> None:
    big = "+x\n" * (MAX_DIFF_CHARS // 8)  # 3/8 of the budget per file
    test_file = file_diff("src/__tests__/a.test.ts", big)
    source = file_diff("src/a.ts", big)
    second_source = file_diff("src/b.ts", big)
    prompt = _prompt(diff=test_file + source + second_source)
    inlined = prompt[prompt.index("```diff") : prompt.rindex("```")]
    assert "b/src/a.ts" in inlined and "b/src/b.ts" in inlined
    assert "a.test.ts" not in inlined
    assert "- src/__tests__/a.test.ts" in prompt
    assert "git diff basesha...HEAD -- <path>" in prompt


def test_a_missing_diff_points_at_git() -> None:
    assert "not available; read it with git" in _prompt(diff=None)


def test_a_first_review_has_no_earlier_round() -> None:
    assert "Earlier round" not in _prompt()


def test_a_later_round_reviews_what_was_pushed_since_not_the_whole_pr_again() -> None:
    delta = file_diff("a.py", "+fixed\n")
    prompt = _prompt(
        diff=file_diff("whole_pr.py"),
        previous=PreviousRound(
            head_sha="prevhead00000000",
            verdict="REQUEST_CHANGES",
            body="1. `a.py:3` drops the error.",
            diff_since=delta,
        ),
    )
    assert "Earlier round: head prevhead0000 got REQUEST_CHANGES" in prompt
    assert "1. `a.py:3` drops the error." in prompt
    assert "which are fixed and which\n  are still open" in prompt
    assert f"Pushed since prevhead0000:\n\n```diff\n{delta.rstrip()}\n```" in prompt
    # The full diff was reviewed last round; every turn would re-read it.
    assert "whole_pr.py" not in prompt
    assert "Full PR diff: not included this round" in prompt


def test_a_rebased_later_round_gets_the_full_diff_again() -> None:
    prompt = _prompt(
        diff=file_diff("whole_pr.py"),
        previous=PreviousRound(
            head_sha="prevhead00000000", verdict="APPROVE", body="No findings.", diff_since=None
        ),
    )
    assert "rebased or force-pushed" in prompt
    assert "Pushed since" not in prompt
    assert "b/whole_pr.py" in prompt
