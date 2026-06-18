from src.github_client import PullRequest
from src.job_store import JobStatus, ReviewJob
from src.prompt import build_review_prompt


def test_prompt_contains_review_policy_and_pr_context() -> None:
    job = ReviewJob(
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
    pr = PullRequest(
        number=9,
        title="Tighten backups",
        body="Adds verification.",
        draft=False,
        state="open",
        author="alice",
        base_ref="main",
        base_sha="basesha",
        head_ref="feature",
        head_sha="headsha",
    )
    prompt = build_review_prompt(
        job=job,
        pr=pr,
        changed_files=["scripts/backup.sh", "scripts/verify.sh"],
        diffstat="2 files changed, 10 insertions(+)",
        ci_summary="checks pending",
    )
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
