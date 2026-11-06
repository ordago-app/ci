"""Shared mechanics for the container-per-review providers.

Codex and Claude are launched the same way — a project-scoped agent image, the
worktree and a per-session config dir mounted in, both chowned to the image's
non-root user — and they answer in the same shape: a transcript whose last
message must carry a VERDICT line. Only the CLI invocation and the config file
differ, so everything else lives here rather than in two copies that drift.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tarfile
from pathlib import Path

from docker.errors import NotFound

# Both agent images run as this non-root user; anything mounted in must be
# owned by it or the CLI cannot write (os error 13).
AGENT_UID = 1000
AGENT_GID = 1000

# The worker is the sole poster. A review container must NOT get a
# write-capable github role, or the model posts its own review via `gh` and
# every PR ends up with duplicate reviews.
REVIEWER_GH_ROLE = "reviewer-readonly"

_VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(APPROVE|REQUEST_CHANGES)\s*$", re.MULTILINE)


def parse_verdict(raw: str) -> tuple[str, str]:
    """Split a review transcript into (body, event).

    Defaults to REQUEST_CHANGES: never approve without an explicit APPROVE
    token, so a truncated or malformed transcript cannot open a merge gate.
    """
    match = _VERDICT_RE.search(raw)
    event = "APPROVE" if (match and match.group(1) == "APPROVE") else "REQUEST_CHANGES"
    body = _VERDICT_RE.sub("", raw).strip()
    return body, event


def repo_git_volume(projects_root: Path, project: str) -> dict[str, dict[str, str]]:
    """The shared clone's `.git`, read-only, at the host path it has outside.

    A review worktree's `.git` is a file naming `<clone>/.git/worktrees/<name>` by
    its host path; without this mount every git command in the container fails
    with "not a git repository" and the reviewer cannot see the diff. Read-only,
    because the PR branch under review must not be able to write hooks or config
    into a clone this service runs git in."""
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


def remove_container(docker_client, name: str) -> None:
    """Best-effort removal that neither raises nor goes quiet.

    Runs in the worker's `finally`, so it must not raise. But a swallowed
    failure leaks one review container per review and eventually exhausts the
    host, so every failure is logged.
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


def remove_container_strict(docker_client, name: str) -> None:
    """Remove the container if it exists, raising if that fails. For callers
    that must not proceed while it may still be there."""
    try:
        container = docker_client.containers.get(name)
    except NotFound:
        return
    container.remove(force=True)


def run_session_container(docker_client, name: str, **run_kwargs):
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


# The review prompt embeds the whole PR diff. As one argv element it fails every
# PR whose diff passes the kernel's 128 KiB single-argument cap ("argument list
# too long"), so it travels into the container as a file and reaches the CLI on
# stdin.
PROMPT_PATH = "/tmp/review-prompt.md"


def prompt_on_stdin(container, prompt: str, argv: list[str]) -> list[str]:
    """Copy `prompt` into the container and return `argv` wrapped to read it on
    stdin. Each argument stays its own element; only the prompt leaves argv."""
    data = prompt.encode("utf-8")
    info = tarfile.TarInfo(Path(PROMPT_PATH).name)
    info.size = len(data)
    info.mode = 0o644
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        tar.addfile(info, io.BytesIO(data))
    container.put_archive(str(Path(PROMPT_PATH).parent), archive.getvalue())
    return ["sh", "-c", f'exec "$@" < {PROMPT_PATH}', "sh", *argv]


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
