"""What every provider's review container needs from the Docker daemon."""

from __future__ import annotations

import concurrent.futures
import io
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from docker import DockerClient
from docker.errors import NotFound

# The prompt can carry a large PR, so it goes into the container as a file and
# reaches the CLI on stdin: as one argv element it failed every large PR with
# "argument list too long" (the kernel caps a single argument at 128 KiB).
PROMPT_PATH = "/tmp/review-prompt.md"


def copy_text_file(container: Any, path: str, text: str) -> None:
    data = text.encode("utf-8")
    info = tarfile.TarInfo(Path(path).name)
    info.size = len(data)
    info.mode = 0o644
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        tar.addfile(info, io.BytesIO(data))
    container.put_archive(str(Path(path).parent), archive.getvalue())


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


def remove_existing(docker: DockerClient, name: str) -> None:
    """Remove the container if it exists, raising if that fails: a session must
    not start while a container under its name may still be there."""
    try:
        container = docker.containers.get(name)
    except NotFound:
        return
    container.remove(force=True)


def remove_quietly(docker: DockerClient, name: str) -> None:
    # Runs in the worker's finally, so it must not raise. But it must not
    # silently swallow failures either: a hidden remove() error leaks one
    # review container per review and eventually exhausts the host.
    try:
        container = docker.containers.get(name)
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
