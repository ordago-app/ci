from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import ToolProfile
from .job_store import ReviewJob


@dataclass(frozen=True)
class SessionRef:
    id: str
    container: str
    worktree: Path


@dataclass(frozen=True)
class ReviewResult:
    body: str
    event: str = "COMMENT"


class ReviewProvider(Protocol):
    def start_review_session(
        self, job: ReviewJob, worktree: Path, profile: ToolProfile
    ) -> SessionRef: ...
    def run_review(
        self, session: SessionRef, prompt: str, timeout_seconds: int
    ) -> ReviewResult: ...
    def cleanup(self, session: SessionRef) -> None: ...
