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


# ── The light image's missing-SDK guard, executed rather than grepped ──
# The rest of this file inspects script text, which cannot tell a real `exit 1`
# from a comment describing one. These two run the entrypoint far enough to take
# each branch. Both stop before any network call: a pre-minted
# RUNNER_REGISTRATION_TOKEN skips the App-credential path, and the guard sits
# above registration.


def _run_entrypoint(tmp_path, *, seed: bool):
    import os
    import subprocess

    sdk_root = tmp_path / "android-sdk"
    seed_dir = tmp_path / "seed"
    if seed:
        (seed_dir / "cmdline-tools" / "latest" / "bin").mkdir(parents=True)
        (seed_dir / "marker.txt").write_text("seeded")

    env = {
        **os.environ,
        "RUNNER_REPOSITORY": "owner/repo",
        "RUNNER_NAME": "test-lane",
        "RUNNER_LABELS": "self-hosted",
        "RUNNER_WORKDIR": str(tmp_path / "work"),
        "RUNNER_EPHEMERAL": "1",
        "RUNNER_REGISTRATION_TOKEN": "token",  # skips App creds, avoids the network
        "SKIP_ANDROID_SDK": "0",  # an SDK-needing class
        "ANDROID_SDK_ROOT": str(sdk_root),
        "ANDROID_SDK_SEED": str(seed_dir),
    }
    proc = subprocess.run(["bash", str(ENTRY)], env=env, capture_output=True, text=True, timeout=60)
    return proc, sdk_root


def test_missing_android_seed_fails_with_a_diagnosis(tmp_path) -> None:
    """An SDK-needing job on a light image must stop with a usable message.

    Without the guard this is `cp: cannot stat '/opt/android-sdk-seed/.'`, which
    reads as a corrupt image rather than a job scheduled onto the wrong host.
    """
    proc, _ = _run_entrypoint(tmp_path, seed=False)

    assert proc.returncode == 1, f"expected exit 1, got {proc.returncode}: {proc.stderr[-500:]}"
    assert "built without the Android SDK" in proc.stderr
    # Name both knobs an operator has to look at, so the message is actionable.
    assert "allowed_classes" in proc.stderr
    assert "runner_image" in proc.stderr
    assert "cannot stat" not in proc.stderr, "the bare cp failure must not be what surfaces"


def test_present_android_seed_is_copied_and_the_guard_does_not_fire(tmp_path) -> None:
    """The guard must not break the normal full-image path it was added beside."""
    proc, sdk_root = _run_entrypoint(tmp_path, seed=True)

    assert "built without the Android SDK" not in proc.stderr
    assert (sdk_root / "marker.txt").read_text() == "seeded", (
        "the seed must still be copied into ANDROID_SDK_ROOT"
    )
