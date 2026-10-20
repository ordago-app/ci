"""A review session whose container start fails must not poison its own retries.

`containers.run` is create-then-start in two Docker API calls. On a loaded host
the start call can time out after the container already exists, so the provider
raises before it has a SessionRef, and the worker's `finally` cleans up only
sessions it was handed. The container stayed behind under the job's name, and
every retry of that job died on `409 Conflict ... name is already in use`.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from docker.errors import APIError, NotFound
from requests.exceptions import ReadTimeout
from src.providers.codex import CodexReviewProvider

from .test_provider_codex import job, profile


class FakeDaemon:
    """Just enough of dockerd's naming rules: a name is unique until removed."""

    def __init__(self) -> None:
        self.containers_by_name: dict[str, MagicMock] = {}
        self.start_fails = False
        self.containers = MagicMock()
        self.containers.get.side_effect = self._get
        self.containers.run.side_effect = self._run
        self.images = MagicMock()
        self.images.get.return_value = MagicMock(id="sha256:image")

    def _get(self, name: str) -> MagicMock:
        if name not in self.containers_by_name:
            raise NotFound(f"No such container: {name}")
        return self.containers_by_name[name]

    def _run(self, *, name: str, **_: object) -> MagicMock:
        if name in self.containers_by_name:
            raise APIError(f'Conflict. The container name "/{name}" is already in use')
        container = MagicMock()
        container.name = name
        container.remove.side_effect = lambda **_: self.containers_by_name.pop(name)
        self.containers_by_name[name] = container  # created ...
        if self.start_fails:  # ... and then the start call times out
            raise ReadTimeout("UnixHTTPConnectionPool: Read timed out. (read timeout=60)")
        return container


def _provider(tmp_path: Path, daemon: FakeDaemon) -> CodexReviewProvider:
    state = tmp_path / "codex-state"
    state.mkdir(exist_ok=True)
    (state / "auth.json").write_text("{}")
    (tmp_path / "worktree").mkdir(exist_ok=True)
    return CodexReviewProvider(
        docker_client=daemon,  # type: ignore[arg-type]
        projects_root=tmp_path / "projects",
        codex_state_root=state,
        router_url="http://claude-router:8000",
        model="",
    )


def test_a_start_that_times_out_leaves_no_container_behind(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    daemon.start_fails = True
    provider = _provider(tmp_path, daemon)

    with pytest.raises(ReadTimeout):
        provider.start_review_session(job(), tmp_path / "worktree", profile())

    assert daemon.containers_by_name == {}


def test_the_retry_after_a_failed_start_gets_a_session(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    daemon.start_fails = True
    provider = _provider(tmp_path, daemon)
    with pytest.raises(ReadTimeout):
        provider.start_review_session(job(), tmp_path / "worktree", profile())

    daemon.start_fails = False
    session = provider.start_review_session(job(), tmp_path / "worktree", profile())

    assert list(daemon.containers_by_name) == [session.container]


def test_a_container_left_under_the_jobs_name_is_replaced_not_conflicted_on(
    tmp_path: Path,
) -> None:
    """Whatever left it there, a container under this job's name cannot be a live
    review: the job is claimed by exactly one worker, which is the caller."""
    daemon = FakeDaemon()
    provider = _provider(tmp_path, daemon)
    name = provider.start_review_session(job(), tmp_path / "worktree", profile()).container
    stale = daemon.containers_by_name[name]

    session = provider.start_review_session(job(), tmp_path / "worktree", profile())

    stale.remove.assert_called_once_with(force=True)
    assert daemon.containers_by_name[session.container] is not stale
