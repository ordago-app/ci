"""The two contracts between the controller and github-review (ADR 0032).

The controller charges github-review's review containers to the RAM budget, finding
them by the label the reviewer stamps on each, and ranks CI by the head states the
reviewer's GET /heads names. Each side keeps its half in its own tree and neither
imports the other, so a rename on one side would silently drop reviews out of the
ledger or turn a tier off. These read the reviewer's source next door.

They came from the first operator's repo, where they ran across its two copies; once
both sides live here, so does the check.
"""

import ast
from pathlib import Path

from src import models
from src.docker_adapter import REVIEW_INSTANCE_LABEL

REVIEWER = Path(__file__).resolve().parents[2] / "github-review" / "src"


def _constant(path: Path, name: str) -> str:
    for node in ast.parse(path.read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
            and isinstance(node.value, ast.Constant)
        ):
            return str(node.value.value)
    raise AssertionError(f"{name} not found in {path}")


def test_the_controller_counts_the_label_the_reviewer_stamps() -> None:
    stamped = _constant(REVIEWER / "providers" / "_agent_container.py", "INSTANCE_LABEL")
    assert stamped == REVIEW_INSTANCE_LABEL


def _head_states_served() -> set[str]:
    tree = ast.parse((REVIEWER / "api.py").read_text())
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_head_state"
    )
    returned = [node.value for node in ast.walk(func) if isinstance(node, ast.Return)]
    # `return "approved" if verdict == "APPROVE" else ...`: only the branches are states.
    branches = [
        branch
        for value in returned
        for branch in ((value.body, value.orelse) if isinstance(value, ast.IfExp) else (value,))
    ]
    return {b.value for b in branches if isinstance(b, ast.Constant)}


def test_the_controller_understands_every_head_state_the_reviewer_serves() -> None:
    understood = {
        value
        for name, value in vars(models).items()
        if name.startswith("REVIEW_") and name != "REVIEW_HELD" and isinstance(value, str)
    }
    assert _head_states_served() == understood
