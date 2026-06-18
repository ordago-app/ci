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
- prioritize correctness, regressions, security, missing tests, and operational risks
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
