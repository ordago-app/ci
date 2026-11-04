from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import ToolProfile
from .job_store import ReviewJob

_VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(APPROVE|REQUEST_CHANGES)\s*$", re.MULTILINE)


class ProviderUnavailableError(RuntimeError):
    """This provider cannot review anything right now, and a retry will not change that.

    Out of quota or credit, not onboarded, a CLI the vendor's API has moved past.
    The worker moves straight to the next provider in the repo's chain. Any other
    exception is treated as transient and retried on this provider first, because
    a fallback costs real credit and a retry does not."""


class ReviewBudgetExceededError(RuntimeError):
    """The review hit its spending cap. Retrying would spend the cap again and stop in
    the same place, so the worker gives up on this head instead."""


@dataclass(frozen=True)
class SessionRef:
    id: str
    container: str
    worktree: Path


@dataclass(frozen=True)
class ReviewResult:
    body: str
    event: str = "COMMENT"


def parse_verdict(text: str) -> ReviewResult:
    """Split a model's final message into the review body and its GitHub event.

    REQUEST_CHANGES unless the text carries an explicit APPROVE token: a review
    that forgot its verdict line must never count as an approval. The last verdict
    wins, because the reviewed PR's title and body are in the prompt and a review
    can quote a verdict line from them before reaching its own."""
    verdicts = _VERDICT_RE.findall(text)
    event = "APPROVE" if verdicts and verdicts[-1] == "APPROVE" else "REQUEST_CHANGES"
    return ReviewResult(body=_VERDICT_RE.sub("", text).strip(), event=event)


class ReviewProvider(Protocol):
    def start_review_session(
        self, job: ReviewJob, worktree: Path, profile: ToolProfile
    ) -> SessionRef: ...
    def run_review(
        self, session: SessionRef, prompt: str, timeout_seconds: int
    ) -> ReviewResult: ...
    def cleanup(self, session: SessionRef) -> None: ...
