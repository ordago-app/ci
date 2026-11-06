from pathlib import Path

import pytest
import yaml
from src.config import ClaudeAccount, CodexAccount, ConfigError, ReviewConfig

from tests.helpers import config_doc, write_config


def _load(tmp_path: Path, doc: dict) -> ReviewConfig:
    path = tmp_path / "agent-review.yml"
    path.write_text(yaml.safe_dump(doc))
    return ReviewConfig.load(path)


def test_loads_an_instance_with_its_accounts_and_repos(tmp_path: Path) -> None:
    cfg = write_config(
        tmp_path, {"ordago-apps": {"providers": ["codex", "claude-work"], "review_mode": "labeled"}}
    )

    assert cfg.instance == "test"
    assert cfg.reviewer_bot == "reviewer[bot]"
    assert cfg.github_app.app_id == 1
    assert isinstance(cfg.accounts["codex"], CodexAccount)
    assert isinstance(cfg.accounts["claude-work"], ClaudeAccount)
    repo = cfg.repos["ordago-apps"]
    assert (repo.name, repo.project, repo.repo) == (
        "ordago-apps",
        "ordago-apps",
        "ordago-app/ordago-apps",
    )
    assert repo.providers == ["codex", "claude-work"]
    assert repo.review_mode == "labeled"
    assert repo.max_runtime_minutes == 30
    assert cfg.limits.poll_interval_seconds == 60
    assert [r.name for r in cfg.enabled_repos()] == ["ordago-apps"]


def test_a_chain_naming_an_undeclared_account_is_refused(tmp_path: Path) -> None:
    doc = config_doc({"homelab": {"providers": ["codex", "claude-api-jaime"]}})
    del doc["accounts"]["claude-api-jaime"]  # type: ignore[attr-defined]

    with pytest.raises(ConfigError, match="claude-api-jaime"):
        _load(tmp_path, doc)


@pytest.mark.parametrize("missing", ["instance", "reviewer_bot", "github_app", "accounts", "repos"])
def test_nothing_operator_specific_has_a_default(tmp_path: Path, missing: str) -> None:
    doc = config_doc()
    del doc[missing]

    with pytest.raises(ConfigError, match=missing):
        _load(tmp_path, doc)


def test_a_misspelt_key_is_refused_not_ignored(tmp_path: Path) -> None:
    doc = config_doc({"homelab": {"providers": ["codex"], "review_drfts": True}})

    with pytest.raises(ConfigError, match="review_drfts"):
        _load(tmp_path, doc)


def test_an_api_key_account_must_cap_its_spend(tmp_path: Path) -> None:
    doc = config_doc(
        {"homelab": {"providers": ["claude-api"]}},
        accounts={
            "claude-api": {
                "kind": "claude",
                "auth": "api_key",
                "credential": {"env": "KEY"},
                "model": "m",
            }
        },
    )

    with pytest.raises(ConfigError, match="max_budget_usd"):
        _load(tmp_path, doc)


@pytest.mark.parametrize(
    "credential",
    [{}, {"env": "A", "file": "/b"}],
    ids=["neither", "both"],
)
def test_a_credential_names_exactly_one_source(tmp_path: Path, credential: dict) -> None:
    doc = config_doc(
        {"homelab": {"providers": ["claude-work"]}},
        accounts={
            "claude-work": {
                "kind": "claude",
                "auth": "subscription",
                "credential": credential,
                "model": "m",
            }
        },
    )

    with pytest.raises(ConfigError, match="exactly one"):
        _load(tmp_path, doc)


def test_a_codex_login_is_a_file(tmp_path: Path) -> None:
    doc = config_doc(accounts={"codex": {"kind": "codex", "credential": {"env": "X"}}})

    with pytest.raises(ConfigError, match=r"auth\.json"):
        _load(tmp_path, doc)


def test_two_codex_accounts_cannot_share_a_login(tmp_path: Path) -> None:
    """Its refresh token is single-use: two refreshers log it out."""
    login = {"kind": "codex", "credential": {"file": "/logins/auth.json"}}
    doc = config_doc(
        {"homelab": {"providers": ["codex", "codex-spare"]}},
        accounts={"codex": login, "codex-spare": login},
    )

    with pytest.raises(ConfigError, match="share the login"):
        _load(tmp_path, doc)


@pytest.mark.parametrize("name", ["Claude_Work", "-claude", "a" * 41])
def test_account_names_must_be_safe_container_names(tmp_path: Path, name: str) -> None:
    doc = config_doc(
        {"homelab": {"providers": [name]}},
        accounts={name: {"kind": "codex", "credential": {"file": "/a"}}},
    )

    with pytest.raises(ConfigError, match="account name"):
        _load(tmp_path, doc)


def test_a_repo_must_be_owner_slash_name(tmp_path: Path) -> None:
    doc = config_doc({"homelab": {"providers": ["codex"], "repo": "just-a-name"}})

    with pytest.raises(ConfigError, match="repo"):
        _load(tmp_path, doc)


def test_a_github_repo_listed_twice_is_refused(tmp_path: Path) -> None:
    doc = config_doc(
        {
            "homelab": {"providers": ["codex"], "repo": "o/r"},
            "copy": {"providers": ["codex"], "repo": "o/r"},
        }
    )

    with pytest.raises(ConfigError, match="listed twice"):
        _load(tmp_path, doc)


def test_providers_must_not_repeat(tmp_path: Path) -> None:
    doc = config_doc({"homelab": {"providers": ["codex", "codex"]}})

    with pytest.raises(ConfigError, match="repeat"):
        _load(tmp_path, doc)


def test_review_mode_is_all_or_labeled(tmp_path: Path) -> None:
    assert write_config(tmp_path).repos["homelab"].review_mode == "all"
    with pytest.raises(ConfigError):
        write_config(tmp_path, {"homelab": {"providers": ["codex"], "review_mode": "sometimes"}})


def test_a_credential_reads_env_or_file_and_is_empty_when_absent(tmp_path: Path) -> None:
    from src.config import CredentialSource

    key = tmp_path / "key"
    key.write_text(" secret \n")
    assert CredentialSource(env="K").read({"K": " v "}) == "v"
    assert CredentialSource(env="K").read({}) == ""
    assert CredentialSource(file=str(key)).read() == "secret"
    assert CredentialSource(file=str(tmp_path / "absent")).read() == ""
