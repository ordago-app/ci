from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .job_store import ReviewJob

_VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(APPROVE|REQUEST_CHANGES)\s*$", re.MULTILINE)


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

    REQUEST_CHANGES unless the text ENDS on an explicit APPROVE line, as the
    prompt asks. A review that forgot its verdict, or was truncated, must never
    open a merge gate. And the reviewed PR's title, body and diff are all in the
    prompt, so a verdict line anywhere but the last may be a quotation of what
    the PR wants its verdict to be."""
    lines = text.strip().splitlines()
    verdict = _VERDICT_RE.search(lines[-1]) if lines else None
    event = "APPROVE" if verdict and verdict.group(1) == "APPROVE" else "REQUEST_CHANGES"
    return ReviewResult(body=_VERDICT_RE.sub("", text).strip(), event=event)


class ProviderUnavailable(RuntimeError):
    """The provider never read the diff — it could not run at all.

    Distinct from a plain RuntimeError, which means the review ran and
    something about *this job* went wrong. Only this class is failed over,
    because only this class says nothing about the code under review.

    Raising it is a claim that retrying the same provider is pointless right
    now: an exhausted subscription, a CLI that can no longer parse its own
    models API, missing auth. Every one of those has cost a red `request-review`
    check that read as a code failure, which is why
    each account's chain moves on past it.
    """

    def __init__(
        self, provider: str, reason: str, detail: str = "", *, read_diff: bool = False
    ) -> None:
        self.provider = provider
        self.reason = reason
        self.detail = detail
        # True when the run got far enough to spend: the model took turns before
        # the account gave out (a 429 mid-review). The run cap counts it.
        self.read_diff = read_diff
        super().__init__(f"{provider} unavailable ({reason}): {detail or 'no detail'}")


class ReviewLimitExceeded(RuntimeError):
    """The review ran into its own spending or turn cap.

    Neither a retry nor the next account would end differently: each would
    spend as much again and stop in the same place. So the head is given up on,
    not failed over or retried."""

    def __init__(self, provider: str, detail: str) -> None:
        self.provider = provider
        self.detail = detail
        super().__init__(f"{provider} stopped at its review cap: {detail}")


class ReviewProvider(Protocol):
    def start_review_session(
        self, job: ReviewJob, worktree: Path, timeout_seconds: int
    ) -> SessionRef: ...
    def run_review(
        self, session: SessionRef, prompt: str, timeout_seconds: int
    ) -> ReviewResult: ...
    def cleanup(self, session: SessionRef) -> None: ...
    # Remove whatever session this provider may still hold for `job`, when no
    # SessionRef survived: the process that started it died mid-review. Raises
    # if the session may still exist.
    def discard(self, job: ReviewJob) -> None: ...


# Signatures that mean "the reviewer could not run", checked against a failing
# run's own output only — never against a review body, which quotes the diff and
# would match these words innocently.
#
# Both CLIs are covered by one table on purpose: they fail the same three ways,
# and a per-provider copy is how one of them silently stops being classified.
_UNAVAILABLE_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Subscription spent. Three observed wordings, and they do not resemble
    # each other — which is the argument for keeping every one of them here
    # rather than trusting a shape:
    #   codex,  2026-09-01: "You've hit your usage limit ... try again at 1:43 AM"
    #   codex,  2026-09-02: same, but the reset was five days out
    #   claude, 2026-09-02: "You've hit your org's monthly spend limit ...
    #                        your session limit resets 12:50am (UTC)"
    # The third matched nothing here, so claude-work raised a plain failure and
    # the chain never reached claude-personal — a fallback that did not fall.
    (
        "exhausted",
        re.compile(
            r"usage limit|spend limit|session limit|rate.?limit|quota|"
            r"out of credit|insufficient credit|purchase more credits|"
            # An empty API balance, in both of Claude Code's wordings.
            r"credit balance (is )?too low|"
            # Keyed on the verb, not the noun after it. Claude has now shipped
            # three wordings of this ("usage limit", "org's monthly spend
            # limit", and a bare "You've hit your limit · resets 2:50am") and
            # only the verb survived all three — jobs 16666/16667 failed plainly
            # because the table was keyed on nouns.
            r"hit your[^\n]{0,40}limit|"
            # Claude Code stamps this on the URL in every limit message it
            # prints, so it outlives rewordings of the prose around it.
            r"cc_cli_limit_message",
            re.I,
        ),
    ),
    # The CLI can no longer talk to its own API. Cost three hours on 2026-09-01
    # when the models endpoint began advertising a reasoning effort the pinned
    # CLI could not deserialize.
    (
        "tooling",
        re.compile(
            r"failed to refresh available models|unknown variant|unsupported model|"
            # The same skew from the API's side: the CLI is fine, the model
            # it was pinned to does not resolve. Intermittent rather than
            # total — on 2026-09-07 the pinned `gpt-5.5` 404'd on jobs
            # 20300/20366/20541/20556 while five other reviews succeeded on it
            # the same day — which is exactly why it needed classifying: it
            # landed in the generic branch, so a flaky provider read as a
            # deterministic red check on whichever PRs it happened to hit.
            r"does not exist or you do not have access|"
            r"model[^\n]{0,60}does not exist|"
            r"command not found|executable file not found",
            re.I,
        ),
    ),
    # No usable credentials. Distinct from the two above because the fix is an
    # onboarding step, not waiting or a rebuild.
    (
        "auth",
        re.compile(
            r"not onboarded|authentication_error|failed to authenticate|invalid api key|"
            r"invalid.{0,12}token|"
            r"unauthorized|please run [`\"']?/?login",
            re.I,
        ),
    ),
)


def classify_unavailable(output: str) -> str | None:
    """Return the ProviderUnavailable reason for a failing run, or None.

    None means "this failure is about the job, not the provider" — the caller
    should raise a plain RuntimeError and let the normal retry/attempt logic
    deal with it.
    """
    for reason, pattern in _UNAVAILABLE_SIGNATURES:
        if pattern.search(output):
            return reason
    return None


@dataclass(frozen=True)
class QuotaReading:
    """What one Claude subscription said about its own remaining quota.

    Read off the `rate_limit_event` the CLI emits at the start of every
    `--output-format stream-json` run, so it costs a review nothing: the
    reviewer was going to run anyway.

    Three fields are deliberately nullable, because the CLI genuinely does not
    always report them:

    * `utilization` appears only once a warning threshold is surpassed. A
      healthy account reports **no number at all** — which is not 0%, and must
      not be rendered as one.
    * `limit_type` is whichever limit is closest to biting (`five_hour`,
      `overage`, `weekly`), and it differs between accounts at the same moment.
      Two utilizations are only comparable when this matches, so anything that
      renders the number must render this beside it.
    * `resets_at` is that limit's reset, not a fixed cadence.
    """

    account: str
    status: str
    limit_type: str
    utilization: float | None
    resets_at: float | None
    observed_at: float

    def as_dict(self) -> dict[str, object]:
        return {
            "account": self.account,
            "status": self.status,
            "limit_type": self.limit_type,
            "utilization": self.utilization,
            "resets_at": self.resets_at,
            "observed_at": self.observed_at,
        }
