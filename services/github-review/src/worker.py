from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ReviewConfig
from .job_store import InterruptedOutcome, JobStatus, ReviewJob, ReviewJobStore
from .poller import CiGreenCache, ReviewPoller
from .prompt import PreviousRound, build_review_prompt
from .provider import (
    ProviderUnavailable,
    QuotaReading,
    ReviewLimitExceeded,
    ReviewProvider,
    ReviewResult,
)

# Opens the body of a carried approval, so a reader of the PR can tell one from a
# review that read the diff. pr-land needs no marker: it counts only
# REQUEST_CHANGES as rounds, and a carried review is always an APPROVE.
CARRIED_REVIEW_MARKER = "<!-- ai-review:carried-approval -->"

# Opens a PR comment saying no review is coming for a head, and why.
NO_REVIEW_MARKER = "<!-- ai-review:no-review -->"

# Every `_budget_spent` reason starts with this, so a budget skip is told apart
# from a `head moved` skip by the row alone.
BUDGET_SKIP_PREFIX = "max review "

# How a job left `running` with no runner behind it reads in `last_error`.
_RESTART_REASON = "interrupted by a restart"
_STUCK_REASON = "stuck running with no runner"


@dataclass(frozen=True)
class ProviderOutage:
    """One provider believed to be down, and since when."""

    provider: str
    reason: str
    detail: str
    since: float

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "reason": self.reason,
            "detail": self.detail,
            "since": self.since,
        }


class ReviewWorker:
    def __init__(
        self,
        *,
        config: ReviewConfig,
        store: ReviewJobStore,
        github,
        worktrees,
        providers: dict[str, ReviewProvider],
        projects_root: Path,
        reviewer_bot: str,
        max_attempts: int = 3,
        max_rounds: int = 5,
        max_runs: int = 10,
        retry_backoff_seconds: float = 0.0,
        concurrency: int = 1,
        max_restart_refunds: int = 2,
        stuck_running_seconds: float = 3 * 3600,
    ) -> None:
        if concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, got {concurrency}")
        if max_restart_refunds < 0:
            raise ValueError(f"max_restart_refunds must be at least 0, got {max_restart_refunds}")
        # A whole failover chain timing out is the longest a review can
        # legitimately run; a stuck timeout inside that would reap live reviews.
        longest_review = max(
            (
                repo.max_runtime_minutes * 60 * len(repo.providers)
                for repo in config.enabled_repos()
            ),
            default=0,
        )
        if stuck_running_seconds <= longest_review:
            raise ValueError(
                f"stuck_running_seconds ({stuck_running_seconds}) must exceed the longest "
                f"possible review ({longest_review} s: max_runtime_minutes x providers)"
            )
        self._config = config
        self._store = store
        self._github = github
        self._worktrees = worktrees
        self._providers = providers
        self._projects_root = projects_root
        self._reviewer_bot = reviewer_bot
        self._max_attempts = max_attempts
        self._max_rounds = max_rounds
        self._max_runs = max_runs
        self._retry_backoff_seconds = retry_backoff_seconds
        self._concurrency = concurrency
        self._max_restart_refunds = max_restart_refunds
        self._stuck_running_seconds = stuck_running_seconds
        self._ci = CiGreenCache(github)
        # Job ids a runner thread of this process is processing right now. A
        # `running` row outside this set has no one to finish it.
        self._held: set[int] = set()
        self._held_lock = threading.Lock()
        # Runners sleep on this until a tick bumps the generation. A counter, not
        # a flag: a bump that lands while a runner is mid-drain is still seen.
        self._wake = threading.Condition()
        self._wake_generation = 0
        # Guards `_outages` and `_last_ok`: written by every runner, read by /status.
        self._health = threading.Lock()
        # Running jobs nobody holds whose sessions are not yet removed, each with
        # the reason it ended up here.
        self._orphans: dict[int, tuple[ReviewJob, str]] = {}
        self._orphans_lock = threading.Lock()
        # Provider -> the outage last seen from it, cleared the moment it
        # reviews again. In memory on purpose: it describes right now, and a
        # restart is exactly when the belief should be re-earned rather than
        # replayed from disk.
        self._outages: dict[str, ProviderOutage] = {}
        # Provider -> when it last completed a review. The counterpart to
        # `_outages`: an outage that clears leaves nothing behind, so without
        # this a recovered provider is indistinguishable from one that has
        # never run. Codex needs it most — it reports no quota at all, so
        # "working as of" is the only positive signal it can offer.
        self._last_ok: dict[str, float] = {}

    @property
    def reviewer_bot(self) -> str:
        return self._reviewer_bot

    def degraded_providers(self) -> list[dict[str, object]]:
        with self._health:
            outages = list(self._outages.values())
        newest_first = sorted(outages, key=lambda o: o.since, reverse=True)
        return [o.as_dict() for o in newest_first]

    def reviewer_quota(self) -> list[dict[str, object]]:
        """One row per provider in the reviewer's chain, for the dashboard.

        Chain order, not sorted: the order IS the failover order, and a card
        that reordered by how spent each account is would hide which one answers
        next. Every field is nullable because the providers genuinely differ in
        what they can report:

        * Claude accounts carry a `quota` reading (see `QuotaReading`), captured
          free from reviews they already ran.
        * Codex carries none. `codex-cli` emits token counts and nothing about
          rate limits (checked on 0.147.0 for both `--json` and
          `--experimental-json`), so the only quota signal it ever gives is the
          exhaustion error itself — which arrives after the fact and is already
          captured in `outage.detail`, reset time and all ("try again at 1:29 PM").
          That is a lagging signal and the card must not dress it up as a level.
        """
        with self._health:
            outages = dict(self._outages)
            last_ok = dict(self._last_ok)
        # Repos may hold different chains, so the first repo to name a provider
        # places it. Registered providers no chain names stay visible, last.
        chained = (name for policy in self._config.enabled_repos() for name in policy.providers)
        rows: list[dict[str, object]] = []
        for name in dict.fromkeys((*chained, *self._providers)):
            provider = self._providers[name]
            reading = getattr(provider, "last_quota", None)
            rows.append(
                {
                    "provider": name,
                    # Capability, not observation: whether this provider can
                    # report quota *at all*, which is fixed by its type. Only
                    # the Claude providers carry `last_quota`. Left to be
                    # inferred downstream from a null reading, it told a Claude
                    # account that had not reviewed since the last restart that
                    # it has no quota API — true of codex alone.
                    "reports_quota": hasattr(provider, "last_quota"),
                    "quota": reading.as_dict() if isinstance(reading, QuotaReading) else None,
                    "outage": (o.as_dict() if (o := outages.get(name)) else None),
                    "last_ok": last_ok.get(name),
                }
            )
        return rows

    def is_degraded(self, repo: str, used: str | None) -> bool:
        """True when this repo's preferred provider is not the one that answered."""
        if used is None:
            with self._health:
                return bool(self._outages)
        for policy in self._config.enabled_repos():
            if policy.repo == repo:
                return policy.providers[0] != used
        return False

    @property
    def max_attempts(self) -> int:
        """Read by /status to tell a retryable failure from one that is out of retries."""
        return self._max_attempts

    @property
    def store(self) -> ReviewJobStore:
        """Read-only access for /status. Public so the API doesn't reach into _store."""
        return self._store

    def start(self) -> None:
        """Take over the queue: recover what a dead process left running, then
        start `concurrency` runner threads. Call once, before the first tick."""
        with self._orphans_lock:
            self._orphans = {
                job.id: (job, _RESTART_REASON)
                for job in self._store.list_by_status(JobStatus.RUNNING)
            }
        self._recover_orphans()
        for index in range(self._concurrency):
            threading.Thread(
                target=self._runner, name=f"review-runner-{index}", daemon=True
            ).start()

    def _reap_stuck(self) -> None:
        """Hand a job running far longer than any review can to orphan recovery.

        Only one no runner thread holds: a runner that is still on it will
        finish it, however late. One nobody holds was lost by this process (a
        runner died outside `_process`'s handler) and would otherwise keep its
        PR unclaimable for good. Claims set `started_at`, so a fresh claim is
        never old enough to be caught between the claim and the hold."""
        cutoff = time.time() - self._stuck_running_seconds
        with self._held_lock:
            held = set(self._held)
        with self._orphans_lock:
            for job in self._store.list_running_since(cutoff):
                if job.id not in held and job.id not in self._orphans:
                    self._orphans[job.id] = (job, _STUCK_REASON)

    def _recover_orphans(self) -> None:
        """Take each running job nobody holds out of `running`, once its sessions are gone.

        A restart does not stop the review containers the dead process started,
        and the requeued job's next session reuses their names: it would fail on
        the conflict before it had a session to clean up, every attempt. So a job
        whose containers cannot be removed stays `running`, which also keeps its
        PR unclaimable, and is tried again on the next tick.

        The first `max_restart_refunds` interruptions of a job cost no attempt.
        Past that it fails like any review, final-attempt comment included, so
        a review that crashes or hangs the service cannot retry forever."""
        with self._orphans_lock:
            for job, reason in list(self._orphans.values()):
                try:
                    for provider in self._providers.values():
                        provider.discard(job)
                    outcome = self._store.recover_interrupted(
                        job.id, self._max_restart_refunds, reason
                    )
                except Exception as exc:
                    print(
                        f"[github-review] job {job.id} stays interrupted: could not remove "
                        f"its review session ({exc}); retrying next tick",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                del self._orphans[job.id]
                if outcome == InterruptedOutcome.REQUEUED:
                    print(
                        f"[github-review] requeued job {job.id} "
                        f"({job.repo}#{job.pr_number}), {reason}",
                        file=sys.stderr,
                        flush=True,
                    )
                elif outcome == InterruptedOutcome.FAILED:
                    failed = self._store.get(job.id)
                    if failed is not None and failed.last_error is not None:
                        self._after_failure(failed, failed.last_error)

    def tick(self) -> None:
        """Enqueue new heads, then wake the runners. Never waits on a review."""
        try:
            self._reap_stuck()
            self._recover_orphans()
            ReviewPoller(
                self._config,
                self._store,
                self._github,
                reviewer_bot=self._reviewer_bot,
                ci=self._ci,
            ).poll_once()
        finally:
            # A failed poll must not also stall retries and explicit requests.
            with self._wake:
                self._wake_generation += 1
                self._wake.notify_all()

    def _runner(self) -> None:
        while True:
            with self._wake:
                seen = self._wake_generation
            try:
                as_of = time.time()
                while self.run_next(as_of):
                    pass
            except Exception as exc:
                # A dead runner would silently shrink the pool for good.
                print(f"[github-review] runner pass failed: {exc!r}", file=sys.stderr, flush=True)
            with self._wake:
                while self._wake_generation == seen:
                    self._wake.wait()

    def run_next(self, as_of: float | None = None) -> bool:
        """Claim the highest-priority job and see it through. False when none is free.

        `as_of` pins the retry clock for one drain, so a job that fails during it
        waits for the next wake rather than spending its attempts back to back."""
        for job in self._store.list_retryable(
            self._max_attempts, self._retry_backoff_seconds, now=as_of
        ):
            if self._store.claim(
                job.id, self._max_attempts, self._retry_backoff_seconds, now=as_of
            ):
                with self._held_lock:
                    self._held.add(job.id)
                try:
                    self._process(job)
                finally:
                    with self._held_lock:
                        self._held.discard(job.id)
                return True
        return False

    def _budget_spent(self, repo: str, pr_number: int) -> str | None:
        """Why this PR gets no further review run, or None while it has budget.

        Two caps, because they bound different things. Rounds bound the
        argument: REQUEST_CHANGES verdicts, so an approval followed by a CI fix
        does not spend it. Runs bound the spend: every provider invocation that
        read the diff, posted or not, so neither approval after approval nor a
        run that keeps failing after it was paid for escapes a ceiling.
        """
        if self._store.rounds_for(repo, pr_number) >= self._max_rounds:
            return f"{BUDGET_SKIP_PREFIX}rounds ({self._max_rounds}) reached"
        if self._store.runs_for(repo, pr_number) >= self._max_runs:
            return f"{BUDGET_SKIP_PREFIX}runs ({self._max_runs}) reached"
        return None

    def _skip_for_budget(self, job: ReviewJob, spent: str) -> None:
        """Skip the head, and say so on the PR the first time its budget runs out.

        Every later head would be skipped the same way. One comment per PR is
        the notice; one per push would be noise."""
        first = (
            self._store.count_skipped_with_reason(job.repo, job.pr_number, BUDGET_SKIP_PREFIX) == 0
        )
        self._store.mark_skipped(job.id, spent)
        if first:
            self._comment(
                job,
                f"{NO_REVIEW_MARKER}\n"
                f"No automated review will run on this PR any more: {spent}. "
                "This is a cost cap, not a finding. A human takes it from here.",
            )

    def _carry_approval(self, job: ReviewJob) -> bool:
        """Re-post the last APPROVE onto `job`'s head when the PR's own diff is unchanged.

        A rebase moves the base under a PR without changing what the PR changes, yet
        every new head SHA used to buy a full review run and a round. The review is
        a judgement of the diff; an identical diff has already been judged. What a
        rebase CAN break — the integration with the moved base — is CI's question,
        and CI re-runs on the new head regardless.

        Only the LATEST posted review may be carried, and only if it approved: an
        older approval followed by findings is not an approval of this PR any more.
        Anything that stops the comparison (a gc'd head, a git error) falls through
        to a normal review — the expensive answer, never a wrong one."""
        previous = self._store.latest_posted_before(job.repo, job.pr_number, job.head_sha)
        if previous is None or previous.verdict != "APPROVE":
            return False
        repo_dir = self._projects_root / job.project / "repo"
        try:
            self._worktrees.fetch(repo_dir, job.repo)
            before = self._worktrees.diff_fingerprint(
                repo_dir=repo_dir, base_sha=previous.base_sha, head_sha=previous.head_sha
            )
            after = self._worktrees.diff_fingerprint(
                repo_dir=repo_dir, base_sha=job.base_sha, head_sha=job.head_sha
            )
            if before is None or before != after:
                return False
            if self._github.get_pull_request(job.repo, job.pr_number).head_sha != job.head_sha:
                return False
            source = previous.carried_from or previous.head_sha
            self._github.post_review(
                job.repo,
                job.pr_number,
                f"{CARRIED_REVIEW_MARKER}\n"
                f"Approval carried over from `{source[:12]}`: this head changes exactly the "
                f"same files to exactly the same contents (diff fingerprint {before[:12]}), "
                "so no new review ran. Only the base moved underneath it, and CI re-verifies "
                "that.",
                "APPROVE",
                commit_id=job.head_sha,
            )
        except Exception as exc:
            print(
                f"[github-review] approval carry-over skipped for "
                f"{job.repo}#{job.pr_number}@{job.head_sha[:12]}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return False
        self._store.mark_carried(job.id, source, previous.provider)
        return True

    def _process(self, job: ReviewJob) -> None:
        """Everything that happens to a claimed job; it leaves `running` before this returns."""
        repo_dir = self._projects_root / job.project / "repo"
        worktree = None
        try:
            repo_policy = next(
                repo for repo in self._config.enabled_repos() if repo.project == job.project
            )
            pr = self._github.get_pull_request(job.repo, job.pr_number)
            if pr.state != "open":
                self._store.mark_skipped(job.id, f"PR {pr.state}")
                return
            if pr.head_sha != job.head_sha:
                self._store.mark_skipped(job.id, f"head moved to {pr.head_sha}")
                return
            if self._carry_approval(job):
                return
            spent = self._budget_spent(job.repo, job.pr_number)
            if spent is not None:
                self._skip_for_budget(job, spent)
                return
            worktree = self._worktrees.prepare(
                repo_dir=repo_dir,
                repo=job.repo,
                project=job.project,
                pr_number=job.pr_number,
                head_sha=job.head_sha,
            )
            prompt = build_review_prompt(
                job=job,
                pr=pr,
                changed_files=self._github.changed_files(job.repo, job.pr_number),
                diffstat=self._github.diffstat(job.repo, job.pr_number),
                ci_summary=self._github.ci_summary(job.repo, job.head_sha)
                if repo_policy.run_ci_first
                else "not requested",
                diff=self._worktrees.pr_diff(
                    repo_dir=repo_dir, base_sha=job.base_sha, head_sha=job.head_sha
                ),
                previous=self._previous_round(job, repo_dir),
            )
            result, used, outages = self._review_with_failover(
                job, worktree, repo_policy.max_runtime_minutes * 60, prompt, repo_policy.providers
            )
            body = self._disclose(result.body, used, repo_policy.providers[0], outages)
            self._github.post_review(
                job.repo, job.pr_number, body, result.event, commit_id=job.head_sha
            )
            self._store.mark_posted(
                job.id, verdict=result.event, provider=used, review_body=result.body
            )
        except ReviewLimitExceeded as exc:
            self._give_up(job, exc)
        except Exception as exc:
            self._store.mark_failed(job.id, str(exc))
            self._after_failure(job, str(exc))
        finally:
            if worktree is not None:
                self._worktrees.cleanup(repo_dir=repo_dir, worktree=worktree)

    def _previous_round(self, job: ReviewJob, repo_dir: Path) -> PreviousRound | None:
        """The review this PR got at its last reviewed head, so this run checks those
        findings and reads what changed since rather than starting over. None when
        there is none to build on: a first review, a carried approval, or a row posted
        before review bodies were kept."""
        previous = self._store.latest_posted_before(job.repo, job.pr_number, job.head_sha)
        if previous is None or previous.review_body is None or previous.verdict is None:
            return None
        return PreviousRound(
            head_sha=previous.head_sha,
            verdict=previous.verdict,
            body=previous.review_body,
            diff_since=self._worktrees.diff_since(
                repo_dir=repo_dir, previous_head_sha=previous.head_sha, head_sha=job.head_sha
            ),
        )

    def _review_with_failover(
        self,
        job: ReviewJob,
        worktree: Path,
        timeout_seconds: int,
        prompt: str,
        chain: list[str],
    ) -> tuple[ReviewResult, str, list[ProviderUnavailable]]:
        """Walk the provider chain until one actually reads the diff.

        Only `ProviderUnavailable` advances the chain. Any other error is about
        this job — a timeout, a bad mount, a malformed transcript — and failing
        over on it would burn the spare account on a problem the spare has too.
        That includes `ReviewLimitExceeded`: the next account would spend as
        much again and stop at the same cap.

        Every outage is collected rather than replaced: when the whole chain is
        down, the operator needs to see that *all* of them were, and why each
        one was, not just whichever failed last.
        """
        outages: list[ProviderUnavailable] = []
        for name in chain:
            provider = self._providers[name]
            session = None
            try:
                session = provider.start_review_session(job, worktree, timeout_seconds)
                try:
                    result = provider.run_review(session, prompt, timeout_seconds=timeout_seconds)
                except ProviderUnavailable:
                    raise
                except Exception:
                    self._store.record_provider_run(job.id)
                    raise
                self._store.record_provider_run(job.id)
                with self._health:
                    self._outages.pop(name, None)
                    self._last_ok[name] = time.time()
                return result, name, outages
            except ProviderUnavailable as exc:
                outages.append(exc)
                with self._health:
                    previous = self._outages.get(name)
                    self._outages[name] = ProviderOutage(
                        provider=exc.provider,
                        reason=exc.reason,
                        detail=exc.detail,
                        # Keep the FIRST sighting: "down since" is the number that
                        # tells a two-minute blip from a five-day quota reset, and
                        # refreshing it on every retry would erase exactly that.
                        since=previous.since if previous else time.time(),
                    )
                self._report_outage(job, exc, bool(chain[chain.index(name) + 1 :]))
            finally:
                if session is not None:
                    provider.cleanup(session)
        raise RuntimeError(
            "no review provider could run: "
            + "; ".join(f"{o.provider} [{o.reason}] {o.detail}" for o in outages)
        )

    @staticmethod
    def _disclose(body: str, used: str, preferred: str, outages: list[ProviderUnavailable]) -> str:
        """Name the reviewer when it was not the preferred one.

        A fallback review attributed to nobody is a review nobody can weigh,
        and — worse — an outage that leaves no trace on the surface everyone
        actually looks at. The banner is the notification: whoever reads the
        review learns in the same breath that the first provider is down and
        needs fixing.
        """
        if used == preferred and not outages:
            return body
        down = ", ".join(f"`{o.provider}` ({o.reason})" for o in outages)
        return (
            f"> ⚠️ Reviewed by **{used}**, not the usual `{preferred}` — {down} "
            f"could not run. This review is real, but the primary reviewer needs fixing.\n\n"
            f"{body}"
        )

    def _report_outage(
        self, job: ReviewJob, exc: ProviderUnavailable, will_fail_over: bool
    ) -> None:
        tail = "failing over to the next provider" if will_fail_over else "no provider left"
        print(
            f"[github-review] provider UNAVAILABLE — {exc.provider} [{exc.reason}] on "
            f"{job.repo}#{job.pr_number} (job {job.id}); {tail}: {exc.detail}",
            file=sys.stderr,
            flush=True,
        )

    def _after_failure(self, job: ReviewJob, error: str) -> None:
        """Log a recorded failure, and say so on the PR when it was the last attempt."""
        self._report_failure(job, error)
        failed = self._store.get(job.id)
        if failed is not None and failed.attempts >= self._max_attempts:
            self._comment(
                job,
                f"{NO_REVIEW_MARKER}\n"
                f"The automated review of `{job.head_sha[:12]}` failed "
                f"{failed.attempts} times and will not be retried on its own: {error}\n\n"
                "Push a new commit, or run `review <pr>`, to try again.",
            )

    def _give_up(self, job: ReviewJob, exc: ReviewLimitExceeded) -> None:
        """Fail the head for good and say so on the PR: a retry would spend the same."""
        self._store.mark_given_up(job.id, str(exc), max_attempts=self._max_attempts)
        self._report_failure(job, str(exc))
        self._comment(
            job,
            f"{NO_REVIEW_MARKER}\n"
            f"The automated review of `{job.head_sha[:12]}` stopped at its cap and will not "
            f"be retried: {exc}\n\n"
            "This is a cost cap, not a finding. Push a new commit, or run `review <pr>`, "
            "to try again.",
        )

    def _report_failure(self, job: ReviewJob, error: str) -> None:
        """Put the failure somewhere a human actually looks.

        Recording it in last_error alone is a silent fallback: the reviewer failed
        every ordago-apps job from 2026-06-21 on a `git worktree add … Permission
        denied` and posted nothing, and `docker logs github-review` stayed clean
        the whole time. Nobody saw it for seven weeks.
        """
        streak = self._store.consecutive_failures(job.repo)
        detail = f"{job.repo}#{job.pr_number}@{job.head_sha[:12]} (job {job.id}): {error}"
        if streak > 1:
            # Distinct wording so a chronically broken repo is greppable on its
            # own — the one-off case must not drown it.
            print(
                f"[github-review] review FAILED — {detail}; "
                f"{streak} consecutive failures for {job.repo} with no review posted since",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(f"[github-review] review FAILED — {detail}", file=sys.stderr, flush=True)

    def _comment(self, job: ReviewJob, body: str) -> None:
        # A notice that fails to post must not turn a recorded outcome into a
        # failed job; the log line is the fallback record.
        try:
            self._github.post_comment(job.repo, job.pr_number, body)
        except Exception as exc:
            print(
                f"[github-review] could not comment on {job.repo}#{job.pr_number}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    def _project_for_repo(self, repo: str) -> str:
        for repo_policy in self._config.enabled_repos():
            if repo_policy.repo == repo:
                return repo_policy.project
        raise KeyError(f"No enabled repo policy configured for {repo!r}")

    def request_review(self, repo: str, pr_number: int) -> ReviewJob:
        """Queue a review of the PR's current head and return its row at once.

        An explicit request reviews regardless of `review_mode` and the draft rule:
        the caller asked for this PR by number. It is idempotent per head. A head
        whose job ran out of attempts gets a fresh set, so asking again is how a
        stuck review is retried. The run cap still bounds what that can spend.

        Raises KeyError for a repo with no enabled policy."""
        project = self._project_for_repo(repo)
        pr = self._github.get_pull_request(repo, pr_number)
        chain = next(r for r in self._config.enabled_repos() if r.project == project).providers
        job = self._store.enqueue(repo, project, chain[0], pr_number, pr.head_sha, pr.base_sha)
        if job.status == JobStatus.FAILED and job.attempts >= self._max_attempts:
            self._store.requeue(job.id)
            job = self._store.get(job.id) or job
        return job
