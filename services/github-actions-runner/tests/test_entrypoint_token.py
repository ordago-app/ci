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


def test_installation_is_resolved_from_the_repo_not_configured() -> None:
    """One App on two accounts has a different installation id under each.

    A configured GITHUB_RUNNER_APP_INSTALLATION_ID pins the whole pool to one
    owner, so a repo that moved to an org can never register a runner."""
    text = ENTRY.read_text()
    assert "GITHUB_RUNNER_APP_INSTALLATION_ID" not in text
    assert (
        "github_repo_installation_api="
        '"https://api.github.com/repos/${RUNNER_REPOSITORY}/installation"'
    ) in text


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


# ── The App-mint path, executed rather than grepped ──
# The assertions above can only see that the script *mentions* the repo
# installation endpoint. They would still pass if the resolved id were thrown
# away and a configured one used for the token exchange, which is the whole bug
# this path exists to prevent. These run the mint for real against stub
# curl/jq/openssl and assert the id that came back from the lookup is the id the
# exchange was addressed to.

_STUBS = {
    "curl": """#!/usr/bin/env python3
import os, sys
argv = sys.argv[1:]
url = [a for a in argv if a.startswith("http")][-1]
with open(os.environ["CURL_LOG"], "a") as fh:
    fh.write(url + chr(10))
status = 200
if url.endswith("/installation"):
    # INSTALLATION_OK_TIMES lookups succeed before INSTALLATION_STATUS applies, so a
    # test can register normally and then fail only the teardown's lookup.
    seen = open(os.environ["CURL_LOG"]).read().count("/installation" + chr(10))
    ok_times = int(os.environ.get("INSTALLATION_OK_TIMES", "0"))
    status = 200 if seen <= ok_times else int(os.environ.get("INSTALLATION_STATUS", "200"))
    body = '{"id": 4242}' if status == 200 else '{"message": "no"}'
elif "/access_tokens" in url:
    body = '{"token": "ghs_from_4242"}'
elif "registration-token" in url:
    body = '{"token": "ARRT"}'
elif "remove-token" in url:
    body = '{"token": "RMT"}'
else:
    body = "{}"
sys.stdout.write(body)
if "-w" in argv:
    # the caller asked for the status code, so a non-2xx is data, not an error
    sys.stdout.write(chr(10) + str(status))
elif status >= 400:
    sys.exit(22)  # what -f does
""",
    "jq": """#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if any(a.startswith("-n") for a in args):
    print('{"jwt": "payload"}')
    sys.exit(0)
print(json.load(sys.stdin).get(args[-1].strip(".'\\""), ""))
""",
    "openssl": """#!/usr/bin/env python3
import base64, sys
if sys.argv[1] == "base64":
    sys.stdout.write(base64.b64encode(sys.stdin.buffer.read()).decode())
else:
    sys.stdout.buffer.write(b"signature-bytes")
""",
}


def _run_mint(
    tmp_path,
    *,
    installation_status: int = 200,
    installation_ok_times: int = 0,
    ephemeral: bool = True,
    config_exit: int = 0,
):
    """Run the entrypoint through registration with no pre-minted token, so it
    takes the App-credential path for real."""
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in _STUBS.items():
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)

    home = tmp_path / "runner"
    home.mkdir()
    (home / "config.sh").write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$@" >> config-calls.txt\n'
        '[ "$1" = remove ] && exit 0\n'
        'printf "%s\\n" "$@" > config-args.txt\n'
        "touch .runner\n"
        'exit "${CONFIG_EXIT:-0}"\n'
    )
    (home / "run.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    for f in ("config.sh", "run.sh"):
        (home / f).chmod(0o755)

    curl_log = tmp_path / "curl.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CURL_LOG": str(curl_log),
        "RUNNER_REPOSITORY": "an-org/ordago-apps",
        "RUNNER_NAME": "test-lane",
        "RUNNER_LABELS": "self-hosted",
        "RUNNER_WORKDIR": str(tmp_path / "work"),
        "RUNNER_EPHEMERAL": "1" if ephemeral else "0",
        "CONFIG_EXIT": str(config_exit),
        "INSTALLATION_STATUS": str(installation_status),
        "INSTALLATION_OK_TIMES": str(installation_ok_times),
        "SKIP_ANDROID_SDK": "1",
        # exported into PATH unconditionally, below the skip guard, under `set -u`
        "ANDROID_SDK_ROOT": str(tmp_path / "sdk"),
        "GITHUB_RUNNER_APP_ID": "9",
        "GITHUB_RUNNER_APP_PRIVATE_KEY_B64": "cGVt",
    }
    env.pop("RUNNER_REGISTRATION_TOKEN", None)
    proc = subprocess.run(
        ["bash", str(ENTRY)], env=env, cwd=home, capture_output=True, text=True, timeout=60
    )
    return proc, curl_log.read_text().splitlines(), home


def test_mint_resolves_the_installation_from_the_repo(tmp_path) -> None:
    proc, urls, _ = _run_mint(tmp_path)

    assert proc.returncode == 0, f"entrypoint failed: {proc.stderr[-800:]}"
    assert "https://api.github.com/repos/an-org/ordago-apps/installation" in urls, (
        "the installation must be looked up from RUNNER_REPOSITORY"
    )


def test_mint_uses_the_resolved_installation_id_not_a_configured_one(tmp_path) -> None:
    """4242 exists nowhere in the script or the environment — it can only reach the
    token exchange by way of the lookup response."""
    _, urls, _ = _run_mint(tmp_path)

    assert "https://api.github.com/app/installations/4242/access_tokens" in urls


def test_mint_registers_with_the_token_it_minted(tmp_path) -> None:
    _, urls, home = _run_mint(tmp_path)

    reg = "https://api.github.com/repos/an-org/ordago-apps/actions/runners/registration-token"
    assert reg in urls
    assert "ARRT" in (home / "config-args.txt").read_text()


def test_mint_stops_with_a_diagnosis_when_the_app_is_not_installed(tmp_path) -> None:
    """A repo whose owner never installed the App must fail at the lookup.

    This is the shape of a half-finished transfer: the repo moved, the App did
    not follow it. Without the guard the lookup yields an empty id and the run
    goes on to exchange a token for installation `` and register with the empty
    string it gets back, so the runner fails later, somewhere else, with a
    message about a bad token rather than about a missing installation.
    """
    proc, urls, home = _run_mint(tmp_path, installation_status=404)

    assert proc.returncode != 0, f"expected a nonzero exit, got 0: {proc.stdout[-500:]}"
    assert "the GitHub App is not installed on the account owning an-org/ordago-apps" in proc.stderr

    # Stopping matters more than the message: neither later call may be reached.
    assert not [u for u in urls if "/access_tokens" in u], (
        "no token may be exchanged once the installation is unknown"
    )
    assert not [u for u in urls if "registration-token" in u]
    assert not (home / "config-args.txt").exists(), "the runner must not have been configured"


def test_mint_does_not_blame_the_installation_for_a_server_error(tmp_path) -> None:
    """A 500 (or a 401 from a bad key, or a rate limit) is not a missing App.

    Both failures stop the runner, so the exit code cannot tell them apart — the
    message is the entire diagnosis, and "install the App" is a dead end when the
    real cause is a revoked private key.
    """
    proc, urls, _ = _run_mint(tmp_path, installation_status=500)

    assert proc.returncode != 0
    assert "failed with HTTP 500" in proc.stderr
    assert "not installed" not in proc.stderr, "a 5xx must not be reported as a missing install"
    # Still refuses to go on with an id it never got.
    assert not [u for u in urls if "/access_tokens" in u]


# ── The static pool's deregistration, executed rather than grepped ──
# Reached by failing config.sh *after* it writes .runner: that leaves the exact
# state cleanup guards (non-ephemeral, registered) and exits before the `exec`
# that would otherwise carry the trap away.


def test_static_runner_deregisters_with_a_freshly_minted_remove_token(tmp_path) -> None:
    proc, urls, home = _run_mint(tmp_path, ephemeral=False, config_exit=1)

    assert proc.returncode != 0
    assert [u for u in urls if u.endswith("/remove-token")], "cleanup must mint a remove-token"
    calls = (home / "config-calls.txt").read_text()
    assert "remove" in calls and "RMT" in calls, (
        "the removal must use the token cleanup just minted"
    )


def test_static_cleanup_skips_removal_when_the_token_cannot_be_minted(tmp_path) -> None:
    """`cleanup` runs under `set +e`, so a failed mint does not stop it — the
    removal has to be skipped explicitly, or config.sh is handed an empty token
    and the failure surfaces as a bogus-credential error during teardown."""
    proc, urls, home = _run_mint(
        tmp_path,
        ephemeral=False,
        config_exit=1,
        installation_status=404,
        installation_ok_times=1,  # registration works; only teardown's lookup fails
    )

    assert proc.returncode != 0
    calls = (home / "config-calls.txt").read_text()
    assert "--name" in calls, "the run must have got as far as registering"
    assert "remove" not in calls, "removal must be skipped, not attempted with an empty token"
    assert not [u for u in urls if u.endswith("/remove-token")], (
        "no remove-token may be requested once its installation lookup failed"
    )
