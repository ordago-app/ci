import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OVERLAYS = [
    REPO / "services" / "claude-code" / "agent-overlay",
    REPO / "services" / "codex-code" / "agent-overlay",
]


def test_review_helper_vendored_and_installed() -> None:
    for overlay in OVERLAYS:
        helper = overlay / "bin" / "review"
        assert helper.is_file(), f"{overlay} missing bin/review"
        assert os.access(helper, os.X_OK), f"{helper} must be executable"
        body = helper.read_text()
        assert "/reviews" in body, "helper must POST to the /reviews trigger"
        assert "VERDICT:" in body, "helper must print the verdict for the agent"
        dockerfile = (overlay / "Dockerfile").read_text()
        assert "bin/review" in dockerfile, f"{overlay}/Dockerfile must install bin/review"


def test_both_overlay_helpers_identical() -> None:
    bodies = [(o / "bin" / "review").read_text() for o in OVERLAYS]
    assert bodies[0] == bodies[1], "review helper must be identical across overlays"
