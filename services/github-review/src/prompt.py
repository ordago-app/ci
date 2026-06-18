from __future__ import annotations

from .github_client import PullRequest
from .job_store import ReviewJob


def build_review_prompt(
    *,
    job: ReviewJob,
    pr: PullRequest,
    changed_files: list[str],
    diffstat: str,
    ci_summary: str,
) -> str:
    files = "\n".join(f"- {path}" for path in changed_files) or "- none reported"
    return f"""You are reviewing PR #{job.pr_number} in {job.repo}.

Review stance:
- prioritize correctness, regressions, security, and operational risks
- ignore cosmetic style unless it hides a bug
- cite repo-relative file paths with line numbers (e.g. `services/foo/bar.py:42`);
  the repo is checked out at /workspace, so never write /workspace-prefixed or
  absolute paths — they won't resolve as links on GitHub
- do not modify the PR
- do not push commits
- do NOT post to GitHub yourself: never run `gh`, `git`, or any write tool;
  the service posts your review for you (posting it yourself causes duplicates)
- output your complete review as your final message — findings with file/line
  references, or a concise "No findings." when the change is clean
- end your final message with a verdict line on its own line, exactly one of:
  `VERDICT: APPROVE` (no required changes; safe for a human to merge) or
  `VERDICT: REQUEST_CHANGES` (there are findings that should be fixed first)
- choose APPROVE only when you found nothing that should block the merge

Test coverage is a required gate, not a preference:
- every behavioral change (new code path, bug fix, changed logic) must ship with
  a test that exercises it — one that would fail if the change were reverted. A
  behavioral change with no such test is a REQUEST_CHANGES finding; name the
  file/function that lacks coverage.
- tests must assert real behavior, not just that a mock was called. A test that
  asserts nothing, or only re-checks its own mock setup, does not count.
- check the obvious edge cases for the changed logic (boundaries, error paths,
  empty/None input) and call out the specific missing case.
- exception — no test required: pure docs/comment/formatting changes, dependency
  or config bumps with no logic, and pure renames/moves already covered by
  existing tests. Say so explicitly rather than inventing a gap.

CI is also a gate: the CI status for the head commit is in the Context below. If
CI is failing, that is a REQUEST_CHANGES finding — point to the failing check. If
the CI status is "unavailable" or "not requested", note it and judge coverage
from the diff; do not treat a missing status as failing.

Context:
- title: {pr.title}
- author: {pr.author}
- body: {pr.body}
- base: {pr.base_ref} @ {job.base_sha}
- head: {pr.head_ref} @ {job.head_sha}
- diffstat: {diffstat}
- CI: {ci_summary}

Changed files:
{files}
"""
