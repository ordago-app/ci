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
- cite file and line references
- do not modify the PR
- do not push commits
- post one GitHub review with findings, or a concise no-findings comment

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
