"""Shared mechanics for the container-per-review providers.

Every review runs in a throwaway container this service starts on its own
Docker host, with the PR's checkout mounted in. That checkout is untrusted —
anyone who can open a PR wrote it — while the container holds an account's
credential. So every review container starts from `sandbox_run_kwargs`: the
image's non-root user, no capabilities, no privilege escalation, the default
bridge network (outbound only, never this service's network), and the checkout
mounted read-only.

Codex and Claude differ only in the CLI they run and what else they mount, so
everything else lives here rather than in two copies that drift.
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import os
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from docker import DockerClient
from docker.errors import NotFound

# Both review images run as this non-root user; anything mounted in that the
# CLI must write is chowned to it.
AGENT_UID = 1000
AGENT_GID = 1000

# Every review container carries this label with its instance's name: two
# instances can share one Docker host, and each reaps only its own.
INSTANCE_LABEL = "ordago.ci.github-review.instance"

WORKSPACE = "/workspace"


def container_name(instance: str, account: str, job_id: int) -> str:
    """Unique per instance, account and job: two instances on one host number
    their jobs independently, and one job may try several accounts."""
    return f"review-{instance}-{account}-{job_id}"


def sandbox_run_kwargs(
    *, instance: str, image_id: str, worktree: Path, environment: dict[str, str]
) -> dict[str, Any]:
    """`containers.run` arguments every review container starts from.

    Callers may add volumes; they must not loosen anything here."""
    return {
        "image": image_id,
        "command": ["sleep", "infinity"],
        "detach": True,
        # Outbound only, to the provider's API. Never this service's own network,
        # where the PR's code could reach the reviewer's API or its neighbours.
        "network": "bridge",
        "user": f"{AGENT_UID}:{AGENT_GID}",
        "working_dir": WORKSPACE,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "labels": {INSTANCE_LABEL: instance},
        "environment": environment,
        "volumes": {str(worktree): {"bind": WORKSPACE, "mode": "ro"}},
    }


def repo_git_volume(projects_root: Path, project: str) -> dict[str, dict[str, str]]:
    """The shared clone's `.git`, read-only, at the host path it has outside.

    A review worktree's `.git` is a file naming `<clone>/.git/worktrees/<name>` by
    its host path; without this mount every git command in the container fails
    with "not a git repository". Read-only, because the PR branch under review
    must not be able to write hooks or config into a clone this service runs git
    in. This is why PROJECTS_ROOT must be mounted at the same path it has on the
    host."""
    git_dir = str(projects_root / project / "repo" / ".git")
    return {git_dir: {"bind": git_dir, "mode": "ro"}}


def make_agent_writable(*paths: Path) -> None:
    for path in paths:
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_dir() and not path.is_symlink():
            for child in path.rglob("*"):
                _chown_for_agent(child)
        _chown_for_agent(path)


def _chown_for_agent(path: Path) -> None:
    try:
        if path.is_symlink():
            os.lchown(path, AGENT_UID, AGENT_GID)
        else:
            os.chown(path, AGENT_UID, AGENT_GID)
    except PermissionError:
        # When running unprivileged (tests/local) the chown is a no-op; in the
        # container github-review is root, so a failure there is real.
        if os.geteuid() == 0:
            raise


def remove_container(docker_client: DockerClient, name: str) -> None:
    """Best-effort removal that neither raises nor goes quiet.

    Runs in the worker's `finally`, so it must not raise. But a swallowed
    failure leaks one review container per review — each holding a credential —
    and eventually exhausts the host, so every failure is logged.
    """
    try:
        container = docker_client.containers.get(name)
    except NotFound:
        return
    except Exception as exc:
        print(
            f"[github-review] could not look up review container {name} for cleanup: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return
    try:
        container.remove(force=True)
    except Exception as exc:
        print(
            f"[github-review] failed to remove review container {name}: {exc}",
            file=sys.stderr,
            flush=True,
        )


def remove_container_strict(docker_client: DockerClient, name: str) -> None:
    """Remove the container if it exists, raising if that fails. For callers
    that must not proceed while it may still be there."""
    try:
        container = docker_client.containers.get(name)
    except NotFound:
        return
    container.remove(force=True)


def reap_review_containers(docker_client: DockerClient, instance: str) -> None:
    """Remove every review container this instance left behind.

    A restart leaves them sleeping forever, each holding a credential in its
    environment. Only safe before the first review starts: it cannot tell a
    leftover from a live session."""
    for container in docker_client.containers.list(
        all=True, filters={"label": f"{INSTANCE_LABEL}={instance}"}
    ):
        remove_container(docker_client, container.name)


def run_session_container(docker_client: DockerClient, name: str, **run_kwargs: Any) -> Any:
    """Start a review container under `name`, never leaving one behind on failure.

    `containers.run` is create-then-start in two API calls. On a loaded host the
    start can time out after the container exists, and the caller raises before
    it has a session for the worker's `finally` to clean up — so the next attempt
    at the same job died on a name conflict, every attempt
    (docs/incidents/2026-10-20-review-container-name-conflict.md).

    A container already under this name is cleared first. That is safe: names
    are per job, and a job is claimed by exactly one worker, the caller.
    """
    remove_container_strict(docker_client, name)
    try:
        return docker_client.containers.run(name=name, **run_kwargs)
    except BaseException:
        remove_container(docker_client, name)
        raise


def copy_text_file(container: Any, path: str, text: str) -> None:
    """Write `text` to `path` inside the container, readable by its user.

    Copied in rather than mounted or written into the worktree: the worktree is
    the code under review and this service runs as root outside it, so a PR
    that commits the target path as a symlink would redirect a host-side write."""
    data = text.encode("utf-8")
    info = tarfile.TarInfo(Path(path).name)
    info.size = len(data)
    info.mode = 0o644
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        tar.addfile(info, io.BytesIO(data))
    container.put_archive(str(Path(path).parent), archive.getvalue())


# The review prompt embeds the whole PR diff. As one argv element it fails every
# PR whose diff passes the kernel's 128 KiB single-argument cap ("argument list
# too long"), so it travels into the container as a file and reaches the CLI on
# stdin.
PROMPT_PATH = "/tmp/review-prompt.md"


def prompt_on_stdin(container: Any, prompt: str, argv: list[str]) -> list[str]:
    """Copy `prompt` into the container and return `argv` wrapped to read it on
    stdin. Each argument stays its own element; only the prompt leaves argv."""
    copy_text_file(container, PROMPT_PATH, prompt)
    return ["sh", "-c", f'exec "$@" < {PROMPT_PATH}', "sh", *argv]


def exec_with_deadline(
    container: Any,
    cmd: list[str],
    *,
    timeout_seconds: int,
    on_timeout: Callable[[], None],
    label: str,
    **exec_kwargs: Any,
) -> Any:
    """`exec_run` has no timeout of its own. `on_timeout` must end the exec (by
    removing the container), or leaving the executor below blocks until it ends."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(container.exec_run, cmd, **exec_kwargs)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError as exc:
            on_timeout()
            raise RuntimeError(f"{label} review timed out") from exc


def diagnostic_lines(output: str, prompt: str) -> str:
    """The CLI's own diagnostics: output lines that are not transcript.

    A review transcript only ever travels inside JSON events, so a line that
    does not parse as JSON is a diagnostic by construction — which is what
    makes it safe to classify. This keeps a provider outage that arrives on
    plain stderr *after* the stream has started (a startup error interleaved
    with events, an exec-layer failure) classifiable, without ever showing the
    classifier the model's prose or the diff.

    Lines echoed from the prompt are dropped: the prompt embeds the PR title
    and body, and a PR *about* rate limits would otherwise classify itself as
    an outage.
    """
    echoed = {line.strip() for line in prompt.splitlines() if line.strip()}
    kept: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line in echoed:
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
    return "\n".join(kept)
