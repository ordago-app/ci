import base64
import json
import time
from pathlib import Path

import yaml
from src.config import ReviewConfig
from src.job_store import ReviewJobStore
from src.worker import ReviewWorker


def tick_and_drain(worker: ReviewWorker) -> None:
    """One poll, then every job claimable as of now, in this thread: what the
    runner threads do after a tick, made deterministic for a test."""
    worker.tick()
    as_of = time.time()
    while worker.run_next(as_of):
        pass


def jwt(exp: float) -> str:
    """An unsigned JWT carrying only `exp`: all the auth owner reads from it."""
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(exp)}).encode()).rstrip(b"=")
    return f"e30.{payload.decode()}.sig"


def login(expires_in: float = 10 * 24 * 3600, refresh_token: str = "rt-1") -> str:
    """auth.json as `codex login` writes it for a ChatGPT account."""
    return json.dumps(
        {
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": "id-1",
                "access_token": jwt(time.time() + expires_in),
                "refresh_token": refresh_token,
                "account_id": "acct",
            },
            "last_refresh": "2026-10-10T00:00:00Z",
        }
    )


def begin_attempt(store: ReviewJobStore, job_id: int) -> None:
    """Put a job into `running` as a runner would, for a test that is setting up
    history rather than exercising the claim."""
    assert store.claim(job_id, max_attempts=99), f"job {job_id} was not claimable"


REPOS = {"homelab": "alvaro-francisco-gil/homelab", "ordago-apps": "ordago-app/ordago-apps"}


def account(name: str) -> dict[str, object]:
    """A declared account for `name`: codex when the name says so, else Claude."""
    if name.startswith("codex"):
        return {"kind": "codex", "credential": {"file": f"/logins/{name}/auth.json"}}
    return {
        "kind": "claude",
        "auth": "subscription",
        "credential": {"env": f"TOKEN_{name.upper().replace('-', '_')}"},
        "model": "claude-test",
    }


def config_doc(
    repos: dict[str, dict[str, object]] | None = None, **top: object
) -> dict[str, object]:
    """A minimal valid agent-review.yml, as a dict. `repos` maps a project to its
    policy overrides; every account a chain names is declared."""
    repos = repos if repos is not None else {"homelab": {"providers": ["codex"]}}
    policies = {
        project: {"repo": REPOS.get(project, f"owner/{project}"), **policy}
        for project, policy in repos.items()
    }
    names = {str(n) for policy in policies.values() for n in policy["providers"]}  # type: ignore[attr-defined]
    doc: dict[str, object] = {
        "instance": "test",
        "reviewer_bot": "reviewer[bot]",
        "github_app": {"app_id": 1, "private_key": {"env": "GITHUB_APP_KEY"}},
        "accounts": top.pop("accounts", None) or {name: account(name) for name in sorted(names)},
        "repos": policies,
    }
    doc.update(top)
    return doc


def write_config(
    tmp_path: Path, repos: dict[str, dict[str, object]] | None = None, **top: object
) -> ReviewConfig:
    path = tmp_path / "agent-review.yml"
    path.write_text(yaml.safe_dump(config_doc(repos, **top)))
    return ReviewConfig.load(path)
