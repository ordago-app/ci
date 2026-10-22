"""An APPROVE carries onto a new head only when the PR's own diff is unchanged.

The case this exists for: a PR is approved, the base moves, the author rebases.
Nothing the reviewer judged has changed, yet every new head SHA used to cost a
full review run and a round of the PR's budget.
"""

import subprocess
from pathlib import Path

from src.git_worktree import GitWorktreeManager
from src.github_client import PullRequest
from src.job_store import JobStatus, ReviewJobStore
from src.provider import ReviewResult, SessionRef
from src.worker import CARRIED_REVIEW_MARKER, ReviewWorker

from tests.test_worker_flow import config

REPO = "alvaro-francisco-gil/homelab"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


# --- the fingerprint ----------------------------------------------------------


def _rebase_onto_moved_base(repo_dir: Path) -> tuple[str, str, str, str]:
    """(old_base, old_head, new_base, rebased_head) after the base gains an unrelated file."""
    old_base = git(repo_dir, "rev-parse", "main")
    old_head = git(repo_dir, "rev-parse", "feature")
    git(repo_dir, "checkout", "-q", "main")
    (repo_dir / "OTHER.md").write_text("the base moved\n")
    git(repo_dir, "add", "OTHER.md")
    git(repo_dir, "commit", "-qm", "base moves")
    new_base = git(repo_dir, "rev-parse", "main")
    git(repo_dir, "checkout", "-q", "feature")
    git(repo_dir, "rebase", "-q", "main")
    return old_base, old_head, new_base, git(repo_dir, "rev-parse", "feature")


def test_a_clean_rebase_keeps_the_fingerprint(tmp_path: Path, repo_dir: Path) -> None:
    mgr = GitWorktreeManager(projects_root=tmp_path)
    old_base, old_head, new_base, rebased = _rebase_onto_moved_base(repo_dir)
    assert rebased != old_head, "precondition: the rebase made a new head"
    before = mgr.diff_fingerprint(repo_dir=repo_dir, base_sha=old_base, head_sha=old_head)
    after = mgr.diff_fingerprint(repo_dir=repo_dir, base_sha=new_base, head_sha=rebased)
    assert before is not None and before == after


def _amend_and_fingerprint(
    repo_dir: Path, mgr: GitWorktreeManager, name: str, data: bytes
) -> str | None:
    (repo_dir / name).write_bytes(data)
    git(repo_dir, "add", name)
    git(repo_dir, "commit", "-qm", f"change {name}")
    return mgr.diff_fingerprint(
        repo_dir=repo_dir, base_sha="main", head_sha=git(repo_dir, "rev-parse", "feature")
    )


def test_a_whitespace_only_change_is_a_different_diff(tmp_path: Path, repo_dir: Path) -> None:
    # Indentation is behaviour in Python; `git patch-id` would call these equal.
    mgr = GitWorktreeManager(projects_root=tmp_path)
    first = _amend_and_fingerprint(repo_dir, mgr, "mod.py", b"if x:\n    y()\nz()\n")
    second = _amend_and_fingerprint(repo_dir, mgr, "mod.py", b"if x:\n    y()\n    z()\n")
    assert first is not None and second is not None and first != second


def test_a_binary_change_is_a_different_diff(tmp_path: Path, repo_dir: Path) -> None:
    mgr = GitWorktreeManager(projects_root=tmp_path)
    first = _amend_and_fingerprint(repo_dir, mgr, "logo.png", b"\x89PNG\x00\x01")
    second = _amend_and_fingerprint(repo_dir, mgr, "logo.png", b"\x89PNG\x00\x02")
    assert first is not None and second is not None and first != second


def test_a_base_edit_to_the_prs_own_file_is_not_carried(tmp_path: Path, repo_dir: Path) -> None:
    # The PR's hunk rebases cleanly and reads the same, but it now sits in a file
    # the base also changed: the before-blob moved, so the fingerprint must too.
    mgr = GitWorktreeManager(projects_root=tmp_path)
    lines = [f"line {i}\n" for i in range(20)]
    git(repo_dir, "checkout", "-q", "main")
    (repo_dir / "long.txt").write_text("".join(lines))
    git(repo_dir, "add", "long.txt")
    git(repo_dir, "commit", "-qm", "long file")
    git(repo_dir, "checkout", "-q", "-b", "pr2")
    (repo_dir / "long.txt").write_text("".join(["PR edit\n", *lines[1:]]))
    git(repo_dir, "commit", "-qam", "pr edits line 0")
    old_base, old_head = git(repo_dir, "rev-parse", "main"), git(repo_dir, "rev-parse", "pr2")
    before = mgr.diff_fingerprint(repo_dir=repo_dir, base_sha=old_base, head_sha=old_head)

    git(repo_dir, "checkout", "-q", "main")
    (repo_dir / "long.txt").write_text("".join([*lines[:19], "base edit\n"]))
    git(repo_dir, "commit", "-qam", "base edits line 19")
    git(repo_dir, "checkout", "-q", "pr2")
    git(repo_dir, "rebase", "-q", "main")
    after = mgr.diff_fingerprint(
        repo_dir=repo_dir, base_sha="main", head_sha=git(repo_dir, "rev-parse", "pr2")
    )
    assert before is not None and after is not None and after != before


def test_a_head_missing_from_the_clone_has_no_fingerprint(tmp_path: Path, repo_dir: Path) -> None:
    mgr = GitWorktreeManager(projects_root=tmp_path)
    assert mgr.diff_fingerprint(repo_dir=repo_dir, base_sha="main", head_sha="0" * 40) is None


# --- the worker ---------------------------------------------------------------


class PRs:
    """A PR whose head the test moves, as a force-push would."""

    def __init__(self) -> None:
        self.head = "h1"
        self.base = "b1"
        self.posted: list[tuple[str, str, str]] = []

    def _pr(self) -> PullRequest:
        return PullRequest(
            number=1,
            title="t",
            body="",
            draft=False,
            state="open",
            author="alice",
            base_ref="main",
            base_sha=self.base,
            head_ref="f",
            head_sha=self.head,
        )

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        return [self._pr()]

    def get_pull_request(self, repo: str, number: int) -> PullRequest:
        return self._pr()

    def changed_files(self, repo: str, number: int) -> list[str]:
        return ["README.md"]

    def diffstat(self, repo: str, number: int) -> str:
        return "1 file changed"

    def ci_summary(self, repo: str, head_sha: str) -> str:
        return "n/a"

    def post_review(self, repo: str, number: int, body: str, event: str, commit_id: str) -> None:
        self.posted.append((body, event, commit_id))


class Worktrees:
    """fingerprints keyed by head; the base is irrelevant to the fake."""

    def __init__(self, tmp: Path, fingerprints: dict[str, str | None]) -> None:
        self.tmp = tmp
        self.fingerprints = fingerprints

    def fetch(self, repo_dir: Path) -> None:
        pass

    def diff_fingerprint(self, *, repo_dir: Path, base_sha: str, head_sha: str) -> str | None:
        return self.fingerprints.get(head_sha)

    def prepare(self, *, repo_dir: Path, project: str, pr_number: int, head_sha: str) -> Path:
        self.tmp.mkdir(parents=True, exist_ok=True)
        return self.tmp

    def cleanup(self, worktree: Path) -> None:
        pass


class CountingProvider:
    def __init__(self, event: str) -> None:
        self.event = event
        self.runs = 0

    def start_review_session(self, job, worktree, profile) -> SessionRef:
        return SessionRef(id=str(job.id), container="c", worktree=worktree)

    def run_review(self, session: SessionRef, prompt: str, timeout_seconds: int) -> ReviewResult:
        self.runs += 1
        return ReviewResult(body="reviewed", event=self.event)

    def cleanup(self, session: SessionRef) -> None:
        pass


def worker(tmp_path: Path, gh: PRs, fingerprints: dict[str, str | None], provider, **kw):
    store = ReviewJobStore(tmp_path / "jobs.db")
    store.init()
    w = ReviewWorker(
        config=config(tmp_path),
        store=store,
        github=gh,
        worktrees=Worktrees(tmp_path / "wt", fingerprints),
        providers={"codex": provider},
        projects_root=tmp_path / "projects",
        reviewer_bot="reviewer[bot]",
        **kw,
    )
    return w, store


def test_an_approval_carries_onto_a_rebased_head_without_a_review_run(tmp_path: Path) -> None:
    gh = PRs()
    provider = CountingProvider("APPROVE")
    w, store = worker(tmp_path, gh, {"h1": "P", "h2": "P"}, provider)
    w.tick()
    assert provider.runs == 1

    gh.head, gh.base = "h2", "b2"
    w.tick()

    assert provider.runs == 1, "the rebased head must not buy a second review run"
    body, event, commit = gh.posted[-1]
    assert (event, commit) == ("APPROVE", "h2")
    assert body.startswith(CARRIED_REVIEW_MARKER), "pr-land keys on this marker"
    assert "h1" in body
    assert store.rounds_for(REPO, 1) == 1, "a carried approval is not a round"
    carried = store.get_posted(REPO, 1, "h2")
    assert carried is not None and carried.carried_from == "h1"


def test_a_changed_diff_is_reviewed_again(tmp_path: Path) -> None:
    gh = PRs()
    provider = CountingProvider("APPROVE")
    w, _ = worker(tmp_path, gh, {"h1": "P", "h2": "Q"}, provider)
    w.tick()
    gh.head = "h2"
    w.tick()
    assert provider.runs == 2
    assert not gh.posted[-1][0].startswith(CARRIED_REVIEW_MARKER)


def test_findings_are_never_carried(tmp_path: Path) -> None:
    gh = PRs()
    provider = CountingProvider("REQUEST_CHANGES")
    w, _ = worker(tmp_path, gh, {"h1": "P", "h2": "P"}, provider)
    w.tick()
    gh.head = "h2"
    w.tick()
    assert provider.runs == 2, (
        "only an APPROVE carries; findings on an identical diff get a fresh look"
    )


def test_an_unknown_fingerprint_falls_back_to_a_review(tmp_path: Path) -> None:
    gh = PRs()
    provider = CountingProvider("APPROVE")
    w, _ = worker(tmp_path, gh, {"h1": None, "h2": None}, provider)
    w.tick()
    gh.head = "h2"
    w.tick()
    assert provider.runs == 2, "no fingerprint is no answer — never read as 'identical'"


def test_a_carry_is_allowed_at_the_round_cap(tmp_path: Path) -> None:
    # The cap is a cost backstop and a carry costs nothing, so a PR that spent its
    # budget and then rebased keeps its approval instead of a synthetic verdict.
    gh = PRs()
    provider = CountingProvider("APPROVE")
    w, store = worker(tmp_path, gh, {"h1": "P", "h2": "P"}, provider, max_rounds=1)
    assert w.run_pr_review(REPO, 1).verdict == "APPROVE"
    gh.head = "h2"
    summary = w.run_pr_review(REPO, 1)
    assert (summary.verdict, summary.escalated) == ("APPROVE", False)
    assert provider.runs == 1
    assert store.get_posted(REPO, 1, "h2") is not None


def test_the_cap_still_refuses_a_real_review(tmp_path: Path) -> None:
    gh = PRs()
    provider = CountingProvider("APPROVE")
    w, store = worker(tmp_path, gh, {"h1": "P", "h2": "Q"}, provider, max_rounds=1)
    w.run_pr_review(REPO, 1)
    gh.head = "h2"
    summary = w.run_pr_review(REPO, 1)
    assert summary.escalated is True
    assert provider.runs == 1
    assert store.list_by_status(JobStatus.SKIPPED), "the refused head is recorded, not left queued"
