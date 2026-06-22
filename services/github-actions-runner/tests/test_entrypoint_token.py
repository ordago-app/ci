from pathlib import Path

ENTRY = Path(__file__).resolve().parents[1] / "entrypoint.sh"


def test_uses_pre_minted_token_when_present() -> None:
    text = ENTRY.read_text()
    assert 'if [ -n "${RUNNER_REGISTRATION_TOKEN:-}" ]' in text
    # When a token is supplied, registration uses it instead of mint_token.
    assert 'registration_token="${RUNNER_REGISTRATION_TOKEN}"' in text


def test_app_creds_optional_when_token_supplied() -> None:
    text = ENTRY.read_text()
    # The hard require_env for App creds must be guarded, not unconditional.
    assert "require_app_creds" in text


def test_ephemeral_skips_remove_mint() -> None:
    text = ENTRY.read_text()
    assert 'if [ "${RUNNER_EPHEMERAL:-0}" != "1" ] && [ -f .runner ]; then' in text


def test_ephemeral_disables_self_update() -> None:
    text = ENTRY.read_text()
    # ephemeral lanes must not self-update mid-job (deadlocks); they pin the image version.
    assert "--disableupdate" in text


def test_ephemeral_reaps_work_dir_on_exit() -> None:
    text = ENTRY.read_text()
    # The controller binds a shared work-dir base and the lane creates a per-lane
    # subdir in it; that host subdir persists after the container auto-removes and
    # accumulates. The cleanup trap must reap it (guarded to the ephemeral path).
    assert 'rm -rf "${RUNNER_WORKDIR}"' in text
