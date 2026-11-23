"""Set RAM aside on a pool host for work the pool does not schedule.

Run inside the scheduler container, which serves the holds API on localhost:

    docker exec ci-scheduler python -m src.hold_cli reserve --name release \\
        --host <host> --gb 9 --ttl 2h --wait 15m
    docker exec ci-scheduler python -m src.hold_cli list
    docker exec ci-scheduler python -m src.hold_cli release --name release

A hold stops NEW lanes landing on its host; lanes already running there finish
normally, so `reserve` waits until the host's lanes and holds fit its budget. Renew a
hold by reserving again under the same name before its TTL runs out. A scheduler
restart drops every hold.

Exit codes: 0 done (for `reserve`: the hold fits), 2 bad arguments or refused by the
scheduler, 69 scheduler unreachable, 75 the hold is in place but lanes were still
draining when --wait ran out.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections.abc import Callable
from typing import Any, TextIO

import httpx

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_UNAVAILABLE = 69
EXIT_STILL_DRAINING = 75

POLL_SECONDS = 10
_ATTEMPTS = 3
_BACKOFF_SECONDS = 1.0
_DURATION = re.compile(r"^(\d+)([hms]?)$")
_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1, "": 1}


class SchedulerUnreachable(RuntimeError):
    pass


class HoldRefused(RuntimeError):
    pass


def parse_duration(text: str) -> int:
    """`2h`, `90m`, `3600s` or bare seconds -> seconds."""
    match = _DURATION.match(text.strip())
    if match is None:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (use e.g. 2h, 90m, 3600s)")
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def format_duration(seconds: int) -> str:
    hours, rest = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s" if secs else f"{minutes}m"
    return f"{secs}s"


def _refusal(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        return response.text
    if isinstance(detail, list):  # pydantic validation errors
        return "; ".join(
            f"{'.'.join(str(p) for p in item.get('loc', [])[1:])}: {item.get('msg')}"
            for item in detail
        )
    return str(detail)


class _HoldsApi:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None,
        sleep: Callable[[float], None],
    ) -> None:
        self._base_url = base_url
        self._client = httpx.Client(base_url=base_url, timeout=5.0, transport=transport)
        self._sleep = sleep

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Every call here is idempotent (PUT upserts, DELETE of a missing hold is a
        no-op), so a transport error or a 5xx is retried, a few times with backoff."""
        last: Exception | None = None
        for attempt in range(_ATTEMPTS):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                last = exc
            else:
                if response.status_code < 400:
                    return response
                if response.status_code < 500:
                    raise HoldRefused(_refusal(response))
                last = RuntimeError(f"HTTP {response.status_code}: {response.text}")
            if attempt + 1 < _ATTEMPTS:
                self._sleep(_BACKOFF_SECONDS * (2**attempt))
        raise SchedulerUnreachable(
            f"ci-scheduler at {self._base_url} did not answer {method} {path} "
            f"after {_ATTEMPTS} attempts: {last}"
        )

    def put(self, name: str, *, host: str, ram_mb: int, ttl_seconds: int) -> dict[str, Any]:
        body = {"host": host, "ram_mb": ram_mb, "ttl_seconds": ttl_seconds}
        return self._request("PUT", f"/holds/{name}", json=body).json()

    def delete(self, name: str) -> None:
        self._request("DELETE", f"/holds/{name}")

    def list(self) -> dict[str, Any]:
        return self._request("GET", "/holds").json()


def _host_line(host: str, ram: dict[str, int]) -> str:
    return (
        f"{host}: lanes {ram['lanes_ram_mb']} MB + held {ram['held_ram_mb']} MB "
        f"of {ram['budget_ram_mb']} MB budget"
    )


def _fits(ram: dict[str, int]) -> bool:
    return ram["lanes_ram_mb"] + ram["held_ram_mb"] <= ram["budget_ram_mb"]


def _reserve(
    api: _HoldsApi,
    args: argparse.Namespace,
    *,
    out: TextIO,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> int:
    ram_mb = args.mb if args.mb is not None else round(args.gb * 1024)
    hold = api.put(args.name, host=args.host, ram_mb=ram_mb, ttl_seconds=args.ttl)
    print(
        f"Hold {hold['name']}: {hold['ram_mb']} MB on {hold['host']} for "
        f"{format_duration(hold['remaining_seconds'])}. New lanes on {hold['host']} "
        "now count it; lanes already running there finish normally.",
        file=out,
    )
    deadline = clock() + args.wait
    last_line = ""
    replaced = False
    while True:
        listing = api.list()
        if not any(h["name"] == args.name for h in listing["holds"]):
            if replaced:
                raise HoldRefused(f"hold {args.name} vanished again right after being placed")
            print(
                f"Hold {args.name} is gone (the scheduler restarted?); placing it again.",
                file=out,
            )
            api.put(args.name, host=args.host, ram_mb=ram_mb, ttl_seconds=args.ttl)
            replaced = True
            continue
        replaced = False
        ram = listing["hosts"][args.host]
        line = _host_line(args.host, ram)
        if _fits(ram):
            print(f"Fits: {line}. Renew before it expires by running reserve again.", file=out)
            return EXIT_OK
        if args.wait == 0:
            print(f"Not waiting: {line}. Lanes there drain as they finish.", file=out)
            return EXIT_OK
        if line != last_line:
            print(f"Waiting for lanes to finish: {line}.", file=out)
            last_line = line
        if clock() >= deadline:
            over = ram["lanes_ram_mb"] + ram["held_ram_mb"] - ram["budget_ram_mb"]
            print(
                f"Gave up waiting after {format_duration(args.wait)}. The hold IS in "
                f"place and no new lanes start on {args.host}, but running lanes are "
                f"still draining ({over} MB over budget). Reserve again to keep waiting, "
                "or release it.",
                file=out,
            )
            return EXIT_STILL_DRAINING
        sleep(min(POLL_SECONDS, deadline - clock()))


def _release(api: _HoldsApi, args: argparse.Namespace, *, out: TextIO) -> int:
    api.delete(args.name)
    print(f"Released hold {args.name}.", file=out)
    return EXIT_OK


def _list(api: _HoldsApi, *, out: TextIO) -> int:
    listing = api.list()
    holds = listing["holds"]
    if holds:
        rows = [("NAME", "HOST", "RAM_MB", "REMAINING")] + [
            (h["name"], h["host"], str(h["ram_mb"]), format_duration(h["remaining_seconds"]))
            for h in holds
        ]
        widths = [max(len(row[i]) for row in rows) for i in range(4)]
        for row in rows:
            cells = (cell.ljust(w) for cell, w in zip(row, widths, strict=True))
            print("  ".join(cells).rstrip(), file=out)
    else:
        print("No holds.", file=out)
    print("", file=out)
    for host, ram in sorted(listing["hosts"].items()):
        print(_host_line(host, ram), file=out)
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.hold_cli",
        description="Set RAM aside on a CI pool host so new lanes stop landing there.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    reserve = commands.add_parser(
        "reserve", help="place or renew a hold, then wait until the host fits it"
    )
    reserve.add_argument("--name", required=True, help="the hold's name; reuse it to renew")
    reserve.add_argument("--host", required=True, help="the pool host to set RAM aside on")
    size = reserve.add_mutually_exclusive_group(required=True)
    size.add_argument("--gb", type=float, help="RAM to set aside, in GiB")
    size.add_argument("--mb", type=int, help="RAM to set aside, in MiB")
    reserve.add_argument(
        "--ttl", required=True, type=parse_duration, help="how long it lasts: 2h, 90m, 3600s"
    )
    reserve.add_argument(
        "--wait",
        type=parse_duration,
        default=parse_duration("15m"),
        help="how long to wait for running lanes to drain (default 15m; 0 = don't wait)",
    )

    release = commands.add_parser("release", help="drop a hold")
    release.add_argument("--name", required=True)

    commands.add_parser("list", help="show every hold and each host's RAM")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    args = _parser().parse_args(argv)
    api = _HoldsApi(
        os.environ.get("CI_SCHEDULER_URL", "http://localhost:8001"),
        transport=transport,
        sleep=sleep,
    )
    try:
        if args.command == "reserve":
            return _reserve(api, args, out=out, sleep=sleep, clock=clock)
        if args.command == "release":
            return _release(api, args, out=out)
        return _list(api, out=out)
    except HoldRefused as exc:
        print(f"Refused by the scheduler: {exc}", file=err)
        return EXIT_REFUSED
    except SchedulerUnreachable as exc:
        print(str(exc), file=err)
        return EXIT_UNAVAILABLE


if __name__ == "__main__":
    sys.exit(main())
