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
