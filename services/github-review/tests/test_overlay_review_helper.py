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


CLAUDE_OVERLAY = REPO / "services" / "claude-code" / "agent-overlay"
CODEX_OVERLAY = REPO / "services" / "codex-code" / "agent-overlay"


def test_respond_to_review_skill_present_for_claude() -> None:
    skill = CLAUDE_OVERLAY / "skills" / "respond-to-review" / "SKILL.md"
    assert skill.is_file(), "claude overlay must ship the respond-to-review skill"
    assert "VERDICT" in skill.read_text()


def test_codex_skills_symlinked_to_claude_single_source() -> None:
    # Codex loads skills natively from $CODEX_HOME/skills; we keep a single
    # source of truth by symlinking the codex overlay's skills/ to the
    # claude-code overlay's skills/ rather than duplicating SKILL.md files.
    codex_skills = CODEX_OVERLAY / "skills"
    assert codex_skills.is_symlink(), "codex overlay skills/ must be a symlink (single source)"
    # The symlink must resolve to the claude overlay's skills dir.
    assert codex_skills.resolve() == (CLAUDE_OVERLAY / "skills").resolve()
    # And resolve through to the actual skill so a build sees real content.
    assert (codex_skills / "respond-to-review" / "SKILL.md").is_file()


def test_codex_dockerfile_wires_skills_into_codex_home() -> None:
    dockerfile = (CODEX_OVERLAY / "Dockerfile").read_text()
    assert "COPY skills /etc/agent-skills" in dockerfile, "codex Dockerfile must install skills"
    # Wired to Codex's documented personal skills dir ($CODEX_HOME/skills).
    assert "/home/agent/.codex/skills" in dockerfile, "codex Dockerfile must link ~/.codex/skills"
