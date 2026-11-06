"""A review session whose container start fails must not poison its own retries.

`containers.run` is create-then-start in two Docker API calls. On a loaded host
the start call can time out after the container already exists, so the provider
raises before it has a SessionRef — and the worker's `finally` cleans up only
sessions it was handed. The created container stayed behind under the job's
name, and every retry of that job died on `409 Conflict ... name is already in
use` (2026-10-20, ordago-apps #1563-#1565: nine never-started codex-review-*
containers on one host, a whole review attempt budget spent on the conflict).
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from docker.errors import APIError, NotFound
from requests.exceptions import ReadTimeout
from src.providers.claude import ClaudeReviewProvider
from src.providers.codex import CodexReviewProvider

from tests.helpers import login

from .test_provider_claude import job as claude_job
from .test_provider_codex import job as codex_job
from .test_provider_codex import profile


class FakeDaemon:
    """Just enough of dockerd's naming rules: a name is unique until removed."""

    def __init__(self) -> None:
        self.containers_by_name: dict[str, MagicMock] = {}
        self.start_fails = False
        self.remove_fails = False
        self.containers = MagicMock()
        self.containers.get.side_effect = self._get
        self.containers.run.side_effect = self._run
        self.images = MagicMock()
        self.images.get.return_value = MagicMock(id="sha256:image")

    def _get(self, name: str) -> MagicMock:
        if name not in self.containers_by_name:
            raise NotFound(f"No such container: {name}")
        return self.containers_by_name[name]

    def _remove(self, name: str) -> None:
        if self.remove_fails:
            raise APIError("removal of container is already in progress")
        self.containers_by_name.pop(name)

    def _run(self, *, name: str, **_: object) -> MagicMock:
        if name in self.containers_by_name:
            raise APIError(f'Conflict. The container name "/{name}" is already in use')
        container = MagicMock()
        container.name = name
        container.remove.side_effect = lambda **_: self._remove(name)
        self.containers_by_name[name] = container  # created ...
        if self.start_fails:  # ... and then the start call times out
            raise ReadTimeout("UnixHTTPConnectionPool: Read timed out. (read timeout=60)")
        return container


def _codex(tmp_path: Path, daemon: FakeDaemon):
    state = tmp_path / "codex-state"
    state.mkdir(exist_ok=True)
    (state / "auth.json").write_text(login())
    provider = CodexReviewProvider(
        docker_client=daemon,
        projects_root=tmp_path / "projects",
        codex_state_root=state,
        router_url="http://claude-router:8000",
        model="",
    )
    return provider, codex_job()


def _claude(tmp_path: Path, daemon: FakeDaemon):
    provider = ClaudeReviewProvider(
        account="claude-work",
        credential_env="CLAUDE_CODE_OAUTH_TOKEN",
        credential="sk-ant-oat01-x",
        docker_client=daemon,
        projects_root=tmp_path / "projects",
        router_url="http://claude-router:8000",
        model="claude-opus-5",
    )
    return provider, claude_job()


PROVIDERS = pytest.mark.parametrize("make", [_codex, _claude], ids=["codex", "claude"])


@PROVIDERS
def test_a_start_that_times_out_leaves_no_container_behind(tmp_path: Path, make) -> None:
    daemon = FakeDaemon()
    daemon.start_fails = True
    provider, job = make(tmp_path, daemon)
    (tmp_path / "worktree").mkdir()

    with pytest.raises(ReadTimeout):
        provider.start_review_session(job, tmp_path / "worktree", profile())

    assert daemon.containers_by_name == {}


@PROVIDERS
def test_the_retry_after_a_failed_start_gets_a_session(tmp_path: Path, make) -> None:
    daemon = FakeDaemon()
    daemon.start_fails = True
    provider, job = make(tmp_path, daemon)
    (tmp_path / "worktree").mkdir()
    with pytest.raises(ReadTimeout):
        provider.start_review_session(job, tmp_path / "worktree", profile())

    daemon.start_fails = False
    session = provider.start_review_session(job, tmp_path / "worktree", profile())

    assert list(daemon.containers_by_name) == [session.container]


@PROVIDERS
def test_a_container_left_under_the_jobs_name_is_replaced_not_conflicted_on(
    tmp_path: Path, make
) -> None:
    """Belt to the braces above: whatever left it there (a crash between create and
    start, an older build without the cleanup), a container under this job's name
    cannot be a live review — the job is claimed by exactly one worker, which is
    the caller."""
    daemon = FakeDaemon()
    provider, job = make(tmp_path, daemon)
    (tmp_path / "worktree").mkdir()
    name = provider.start_review_session(job, tmp_path / "worktree", profile()).container
    stale = daemon.containers_by_name[name]

    session = provider.start_review_session(job, tmp_path / "worktree", profile())

    stale.remove.assert_called_once_with(force=True)
    assert daemon.containers_by_name[session.container] is not stale


@PROVIDERS
def test_a_stale_container_that_cannot_be_removed_aborts_the_start(tmp_path: Path, make) -> None:
    """Starting anyway would only hit the name conflict, and would hide why."""
    daemon = FakeDaemon()
    provider, job = make(tmp_path, daemon)
    (tmp_path / "worktree").mkdir()
    name = provider.start_review_session(job, tmp_path / "worktree", profile()).container
    stale = daemon.containers_by_name[name]
    daemon.remove_fails = True
    runs = daemon.containers.run.call_count

    with pytest.raises(APIError, match="already in progress"):
        provider.start_review_session(job, tmp_path / "worktree", profile())

    assert daemon.containers.run.call_count == runs
    assert daemon.containers_by_name == {name: stale}

    daemon.remove_fails = False
    session = provider.start_review_session(job, tmp_path / "worktree", profile())
    assert daemon.containers_by_name[session.container] is not stale


@PROVIDERS
def test_a_cleanup_failure_after_a_failed_start_keeps_the_start_error(tmp_path: Path, make) -> None:
    """The caller must see why the start failed, not the cleanup's own trouble; and
    the leftover is then cleared by the next attempt's stale-name removal."""
    daemon = FakeDaemon()
    daemon.start_fails = True
    daemon.remove_fails = True
    provider, job = make(tmp_path, daemon)
    (tmp_path / "worktree").mkdir()

    with pytest.raises(ReadTimeout):
        provider.start_review_session(job, tmp_path / "worktree", profile())
    assert len(daemon.containers_by_name) == 1

    daemon.start_fails = False
    daemon.remove_fails = False
    session = provider.start_review_session(job, tmp_path / "worktree", profile())
    assert list(daemon.containers_by_name) == [session.container]
