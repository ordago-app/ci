"""No code path may silently choose one operator's machine.

These defaults were harmless while this service lived in a single operator's
private repo: an unset host meant `powerserver` because there was only ever
`powerserver`. The federated pool makes them a placement bug with a blast
radius — a second operator's dispatcher, deployed with incomplete config,
would quietly put that operator's jobs on Álvaro's hardware and the ledger
would record it as intended. See docs/plans/ready/ci-platform-extraction.md,
Task 2.

Every host in this file is deliberately NOT a real one. A test that asserts
"the default is not powerserver" by naming powerserver as its expected value
would pass for the wrong reason.
"""

import os
from dataclasses import fields
from unittest.mock import patch

import pytest
from src.config import ControllerConfig
from src.models import AdmitDecision, Reservation

from .conftest import VALID_CONFIG

OPERATOR_MACHINES = ("powerserver", "powervaro-ci")


def test_config_requires_an_explicit_default_host(write_config) -> None:
    """`default_host` selects the machine work lands on when `hosts:` is absent.

    A platform shared by two operators cannot ship a guess for it."""
    without_default_host = "\n".join(
        line for line in VALID_CONFIG.splitlines() if not line.startswith("default_host:")
    )
    assert "default_host" not in without_default_host
    with pytest.raises(Exception) as exc:
        ControllerConfig.load(write_config(without_default_host))
    assert "default_host" in str(exc.value)


def test_runner_host_is_required_rather_than_assumed(write_config) -> None:
    """RUNNER_HOST names which pool member this dispatcher is running on.

    Defaulting it meant a dispatcher started without it claimed to be
    `powerserver` and reaped `powerserver`'s lanes as its own."""
    from src.main import main

    with (
        patch.dict(os.environ, {"CI_CONTROLLER_CONFIG": "/nonexistent"}, clear=True),
        pytest.raises(SystemExit) as exc,
    ):
        main()
    assert "RUNNER_HOST" in str(exc.value)


@pytest.mark.parametrize("model", [AdmitDecision, Reservation])
def test_placement_records_do_not_default_to_an_operator_machine(model) -> None:
    """A record describing where a lane went must not invent an answer."""
    default = next(f for f in fields(model) if f.name == "host").default
    assert default not in OPERATOR_MACHINES, (
        f"{model.__name__}.host defaults to {default!r}; an unset host must be "
        "falsy so callers fall back explicitly, never a real machine"
    )
    assert not default, f"{model.__name__}.host default must be falsy, got {default!r}"


def test_controller_requires_the_host_it_is_running_on() -> None:
    """Controller compares reservation hosts against its own to decide what it
    owns. Guessing that identity is how a dispatcher reaps a peer's lanes."""
    import inspect

    from src.controller import Controller

    param = inspect.signature(Controller.__init__).parameters["host"]
    assert param.default is inspect.Parameter.empty, (
        "Controller.host must be required; a dispatcher that does not know which "
        "machine it runs on cannot safely decide which lanes are its own"
    )


def test_no_operator_machine_is_hardcoded_as_a_default_in_source() -> None:
    """Backstop for the four assertions above: catches a sixth site appearing.

    Scoped to the quoted string literal, which catches both `= "powerserver"`
    and `os.environ.get(..., "powerserver")`. Prose is untouched: the comments
    and incident references naming real hosts are this repo's memory and are
    deliberately kept, and they do not quote the name."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src"
    pattern = re.compile('"(' + "|".join(OPERATOR_MACHINES) + ')"')
    offenders = [
        f"{p.name}:{i}"
        for p in sorted(src.glob("*.py"))
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert not offenders, f"operator machine hardcoded as a default at: {offenders}"
