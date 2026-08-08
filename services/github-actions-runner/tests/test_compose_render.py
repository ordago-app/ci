"""Render compose.yml.j2 from the committed pool config and assert the pool
shape — the heavy/light split, KVM, SSD work dirs, and SKIP_ANDROID_SDK that
the hand-written compose.yml used to encode by copy-paste.

Mirrors ansible's Jinja settings (trim_blocks=True, lstrip_blocks=False) so the
rendered text matches what `ansible.builtin.template` writes on the host.
"""

from pathlib import Path

import jinja2
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = REPO_ROOT / "services/github-actions-runner/compose.yml.j2"
POOL_CONFIG = REPO_ROOT / "personal/github-runners.yml"
HOST = "powerserver"


def render(respect_enabled: bool = False) -> dict:
    """Render the compose template from the committed pool config.

    The template only emits runners with ``enabled: true``. These tests assert
    the *template's* handling of each runner archetype (heavy/light, KVM, SSD
    work dirs, ephemeral, per-runner repository) — which is independent of which
    runners happen to be enabled for deployment. So by default we force every
    configured runner enabled. Pass ``respect_enabled=True`` to render the
    actual deployed pool (honouring the real ``enabled`` flags).
    """
    config = yaml.safe_load(POOL_CONFIG.read_text())
    runners_cfg = config["github_actions_runners"]
    if not respect_enabled:
        runners_cfg = dict(runners_cfg)
        runners_cfg["runners"] = [{**r, "enabled": True} for r in runners_cfg["runners"]]
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE.parent)),
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )
    text = env.get_template(TEMPLATE.name).render(
        github_actions_runners=runners_cfg,
        inventory_hostname=HOST,
    )
    return yaml.safe_load(text)


def test_renders_valid_yaml_with_seven_runners():
    # Template-shape view (all configured runners forced enabled).
    compose = render()
    assert set(compose) == {"services", "networks"}
    # 5 ordago runners + 2 homelab-ci runners
    assert len(compose["services"]) == 7


def test_live_config_deploys_no_static_runners():
    """The deployed pool (real ``enabled`` flags) is now empty.

    The static homelab pool was retired in `bc4c2f0` when CI cut over to
    ci-controller, which spawns ephemeral lanes on demand instead; the ordago
    runners stay held for capacity. So every runner in the committed config is
    disabled and the template emits a `services:` key with nothing under it.

    Ansible never deploys that: the compose-render and stack-up tasks are gated
    on `runners | selectattr('enabled') | length > 0`, because docker compose
    rejects a services-less file. This test asserts the config side of that
    invariant — if a runner is ever re-enabled by hand it fires, forcing a
    decision about whether a static runner should coexist with ci-controller's
    dynamic lanes (they would compete for the same labels).

    (This test previously asserted `{"homelab-ci-1", "homelab-ci-2"}` and had
    been failing since the cutover — nothing ran it, as `services/*/tests`
    outside the pytest.yml matrix had no CI job.)
    """
    assert render(respect_enabled=True)["services"] is None


def test_runner_names_and_labels():
    services = render()["services"]
    heavy = services["ordago-android-e2e"]
    assert heavy["container_name"] == "github-runner-ordago-android-e2e"
    env = heavy["environment"]
    assert env["RUNNER_NAME"] == f"{HOST}-ordago-android-e2e"
    assert env["RUNNER_REPOSITORY"] == "alvaro-francisco-gil/ordago-apps"
    # self-hosted,linux,x64 are prepended; android-e2e routes the emulator job.
    assert env["RUNNER_LABELS"].split(",") == [
        "self-hosted",
        "linux",
        "x64",
        "powerserver",
        "android-e2e",
        "ordago-ci",
    ]
    # Light runners carry ordago-ci but never android-e2e.
    for name in ("ordago-android-e2e-2", "ordago-android-e2e-5"):
        labels = services[name]["environment"]["RUNNER_LABELS"].split(",")
        assert "ordago-ci" in labels
        assert "android-e2e" not in labels


def test_only_heavy_runner_has_kvm_and_kvm_group():
    services = render()["services"]
    assert services["ordago-android-e2e"]["devices"] == ["/dev/kvm:/dev/kvm"]
    assert services["ordago-android-e2e"]["group_add"] == ["994"]
    for name in (
        "ordago-android-e2e-2",
        "ordago-android-e2e-3",
        "ordago-android-e2e-4",
        "ordago-android-e2e-5",
    ):
        assert "devices" not in services[name]
        assert "group_add" not in services[name]


def test_only_lean_light_runners_skip_android_sdk():
    services = render()["services"]
    for name in ("ordago-android-e2e-4", "ordago-android-e2e-5"):
        assert services[name]["environment"]["SKIP_ANDROID_SDK"] == "1"
    for name in (
        "ordago-android-e2e",
        "ordago-android-e2e-2",
        "ordago-android-e2e-3",
    ):
        assert "SKIP_ANDROID_SDK" not in services[name]["environment"]


def test_light_runner_work_dirs_live_on_the_ssd():
    services = render()["services"]
    # Heavy work dir stays on the HDD; light work dirs move to /mnt/ci-ssd so
    # concurrent random I/O does not saturate the spinning disk.
    heavy_work = next(
        v for v in services["ordago-android-e2e"]["volumes"] if v.endswith(":/runner-work:rw")
    )
    assert heavy_work.startswith("/opt/personal/")
    for name in (
        "ordago-android-e2e-2",
        "ordago-android-e2e-3",
        "ordago-android-e2e-4",
        "ordago-android-e2e-5",
    ):
        work = next(v for v in services[name]["volumes"] if v.endswith(":/runner-work:rw"))
        assert work.startswith("/mnt/ci-ssd/")


def test_light_runners_share_one_ssd_pnpm_store():
    services = render()["services"]

    # The mounted pnpm cache volume only persists across jobs if pnpm's store-dir
    # actually points at it (the image sets store-dir=/cache/pnpm). For the light
    # pool, node_modules lives in /runner-work on the SSD, so the store must be on
    # the SAME disk (/mnt/ci-ssd) for pnpm to hardlink instead of copy across
    # disks. A single shared store maximizes cache hits and fits the SSD budget.
    def pnpm_host(name: str) -> str:
        return next(
            v.rsplit(":/cache/pnpm:", 1)[0]
            for v in services[name]["volumes"]
            if ":/cache/pnpm:" in v
        )

    light = [f"ordago-android-e2e-{n}" for n in (2, 3, 4, 5)]
    hosts = {pnpm_host(name) for name in light}
    assert hosts == {"/mnt/ci-ssd/pnpm-store"}, (
        "light runners must share one pnpm store on the SSD (same disk as /runner-work)"
    )
    # Heavy's work dir is on the HDD, so its store stays on the HDD to keep
    # hardlinks on the same filesystem.
    assert pnpm_host("ordago-android-e2e").startswith("/opt/personal/")


def test_image_points_pnpm_store_at_the_mounted_volume():
    # Regression: PNPM_HOME=/cache/pnpm only sets pnpm's global bin, not the
    # store, so without store-dir pnpm kept its store on the container layer and
    # the mounted volume sat empty/unused. A global .npmrc store-dir wires it up.
    dockerfile = (REPO_ROOT / "services/github-actions-runner/Dockerfile").read_text()
    assert "store-dir=/cache/pnpm" in dockerfile, (
        "image must set pnpm store-dir to the mounted /cache/pnpm volume"
    )


def test_every_runner_runs_an_init_to_reap_zombies():
    # run.sh is PID 1 and CI jobs spawn many git/node children; without an init
    # their orphaned grandchildren accumulate as zombies (same class of leak as
    # mcp-gateway / github-review). tini reaps and forwards signals to run.sh.
    services = render()["services"]
    for name, svc in services.items():
        assert svc.get("init") is True, f"{name} must set init: true to reap CI-job zombies"


def test_secret_env_file_and_external_network():
    compose = render()
    for svc in compose["services"].values():
        assert svc["env_file"] == [
            {"path": "/opt/personal/secrets/github-actions-runner.env", "required": False}
        ]
        assert svc["networks"] == ["homelab"]
    assert compose["networks"]["homelab"]["external"] is True


# ── Tests for per-runner repository override + ephemeral flag (Stage B) ──


def test_ordago_runner_uses_top_level_repository():
    """Ordago runners have no per-runner repository; they should use the top-level default."""
    services = render()["services"]
    for name in (
        "ordago-android-e2e",
        "ordago-android-e2e-2",
        "ordago-android-e2e-3",
        "ordago-android-e2e-4",
        "ordago-android-e2e-5",
    ):
        repo = services[name]["environment"]["RUNNER_REPOSITORY"]
        assert repo == "alvaro-francisco-gil/ordago-apps"


def test_homelab_runner_overrides_repository():
    """Homelab runners declare repository: alvaro-francisco-gil/homelab; that must win."""
    services = render()["services"]
    for name in ("homelab-ci-1", "homelab-ci-2"):
        repo = services[name]["environment"]["RUNNER_REPOSITORY"]
        assert repo == "alvaro-francisco-gil/homelab"


def test_homelab_runner_is_ephemeral():
    """Homelab runners have ephemeral: true; compose must set RUNNER_EPHEMERAL=1."""
    services = render()["services"]
    assert services["homelab-ci-1"]["environment"]["RUNNER_EPHEMERAL"] == "1"
    assert services["homelab-ci-2"]["environment"]["RUNNER_EPHEMERAL"] == "1"


def test_ordago_runner_has_no_runner_ephemeral():
    """Ordago runners do not set ephemeral; RUNNER_EPHEMERAL must be absent."""
    services = render()["services"]
    for name in (
        "ordago-android-e2e",
        "ordago-android-e2e-2",
        "ordago-android-e2e-3",
        "ordago-android-e2e-4",
        "ordago-android-e2e-5",
    ):
        assert "RUNNER_EPHEMERAL" not in services[name]["environment"]


def test_homelab_runner_labels_include_self_hosted_and_homelab():
    """Homelab runner labels must include the standard self-hosted prefix and the homelab label."""
    services = render()["services"]
    for name in ("homelab-ci-1", "homelab-ci-2"):
        labels = services[name]["environment"]["RUNNER_LABELS"].split(",")
        assert "self-hosted" in labels
        assert "homelab" in labels
