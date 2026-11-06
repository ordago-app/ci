"""One reviewer instance's configuration: who it is, what it may spend, what it reviews.

Everything an instance differs by lives in this one file, so the same code runs
as any operator's reviewer on any Docker host. Nothing here defaults to one
operator's machine, account or bot: what has no safe default is required, and a
config that names something it never declared is refused at startup — not at
2am, on the failover nobody has exercised.

Secrets never appear in the file. A credential is a reference — an environment
variable or a file path inside the service container — resolved when a review
needs it, so a rotated key is picked up without a restart and a missing one
fails over (see `CredentialSource.read`).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# Instance and account names end up in Docker container names and labels, so
# they keep to what both accept.
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"
_REPO_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"


class ConfigError(ValueError):
    pass


class _Strict(BaseModel):
    # A misspelt key is a setting silently left at its default. Refuse it.
    model_config = ConfigDict(extra="forbid", frozen=True)


class CredentialSource(_Strict):
    """Where a secret is read from: exactly one of an env var or a file.

    Paths are inside the service container. The value is never logged."""

    env: str | None = None
    file: str | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> CredentialSource:
        if (self.env is None) == (self.file is None):
            raise ValueError("a credential names exactly one of `env:` or `file:`")
        return self

    def describe(self) -> str:
        return f"env {self.env}" if self.env is not None else f"file {self.file}"

    def read(self, environ: Mapping[str, str] | None = None) -> str:
        """The secret, stripped, or "" when it is unset, empty or unreadable.

        "" rather than an exception on purpose: for a review account, a missing
        credential is an outage of that account, and the chain moves on past it."""
        if self.env is not None:
            return (environ if environ is not None else os.environ).get(self.env, "").strip()
        assert self.file is not None
        try:
            return Path(self.file).read_text().strip()
        except OSError:
            return ""


class ClaudeAccount(_Strict):
    """One Anthropic account. Every one runs in the hardened claude-review container.

    `auth` decides who pays and which variable carries the secret:
    `subscription` is a Claude plan's OAuth token from `claude setup-token`
    (CLAUDE_CODE_OAUTH_TOKEN, spends the plan's limits); `api_key` is a Console
    key (ANTHROPIC_API_KEY, spends API credit). A container gets exactly one."""

    kind: Literal["claude"]
    auth: Literal["subscription", "api_key"]
    credential: CredentialSource
    model: str = Field(min_length=1)
    # Hard cap on what one review may spend, passed to the CLI. Required for an
    # API key: that account pays per token, and a PR is untrusted input that can
    # ask for a very long review.
    max_budget_usd: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def api_keys_are_capped(self) -> ClaudeAccount:
        if self.auth == "api_key" and self.max_budget_usd is None:
            raise ValueError("an api_key account needs max_budget_usd: it spends real credit")
        return self


class CodexAccount(_Strict):
    """One ChatGPT login for the codex CLI.

    `credential.file` is that login's `auth.json`. The service refreshes it in
    place (its refresh token is single-use), so it must be writable and no other
    process may refresh the same login — see providers/codex_auth.py."""

    kind: Literal["codex"]
    credential: CredentialSource
    # Empty = unpinned: the CLI uses whatever the subscription serves. Pinning
    # an id broke twice when OpenAI rotated its allowlist.
    model: str = ""

    @field_validator("credential")
    @classmethod
    def login_is_a_file(cls, value: CredentialSource) -> CredentialSource:
        if value.file is None:
            raise ValueError("a codex account's credential is its auth.json `file:`")
        return value


Account = Annotated[ClaudeAccount | CodexAccount, Field(discriminator="kind")]


class GitHubApp(_Strict):
    """The GitHub App the reviewer acts as: it reads PRs, fetches code, posts reviews.

    Install it on every account that owns a reviewed repo, with Pull requests:
    write, Contents: read, Checks: read, Metadata: read."""

    app_id: int = Field(gt=0)
    private_key: CredentialSource


class RepoPolicy(_Strict):
    # Filled from the map key: the key is the project slug, which names this
    # repo's clone and worktrees on disk.
    name: str = ""
    project: str = ""
    repo: str = Field(pattern=_REPO_PATTERN)
    # Ordered failover chain of account names, not a single choice. The first
    # entry does the review; a later one is tried only when the one before it
    # could not run at all (ProviderUnavailable). A one-entry list is a repo
    # that opts out of failover, which is a config decision, not a code path.
    providers: list[str] = Field(min_length=1)
    enabled: bool = True
    review_drafts: bool = False
    review_dependency_bots: bool = False
    run_ci_first: bool = True
    review_mode: Literal["all", "labeled"] = "all"
    max_runtime_minutes: int = Field(default=30, ge=1)

    @field_validator("providers")
    @classmethod
    def providers_are_unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("providers must not repeat — a chain retries accounts, not itself")
        return value


class Limits(_Strict):
    """How hard the instance works, and how much any one PR may cost."""

    poll_interval_seconds: int = Field(default=60, ge=1)
    # Reviews in flight at once, on any account, oldest PR first. Each is its
    # own container (~600 MB); one PR runs one at a time.
    concurrency: int = Field(default=3, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    # A failed review waits this long before its next attempt, independent of
    # the poll, so an outage does not spend every attempt in minutes.
    retry_backoff_seconds: int = Field(default=900, ge=0)
    # REQUEST_CHANGES verdicts per PR: the argument the cap bounds.
    max_rounds: int = Field(default=5, ge=1)
    # Provider invocations per PR, posted or not: the spend the cap bounds.
    max_runs: int = Field(default=10, ge=1)
    # A review interrupted by a restart gets its attempt back this many times;
    # after that the interruption counts, so a review that crashes or hangs the
    # service cannot retry forever.
    max_restart_refunds: int = Field(default=2, ge=0)
    # A job `running` this long with no runner holding it is treated as
    # interrupted. Must exceed max_runtime_minutes x the longest chain; the
    # worker refuses to start otherwise.
    stuck_running_seconds: int = Field(default=10800, ge=1)


class _RawConfig(_Strict):
    instance: str = Field(pattern=NAME_PATTERN)
    reviewer_bot: str = Field(min_length=1)
    github_app: GitHubApp
    accounts: dict[str, Account] = Field(min_length=1)
    repos: dict[str, RepoPolicy]
    limits: Limits = Limits()


@dataclass(frozen=True)
class ReviewConfig:
    instance: str
    reviewer_bot: str
    github_app: GitHubApp
    accounts: dict[str, ClaudeAccount | CodexAccount]
    repos: dict[str, RepoPolicy]
    limits: Limits

    @classmethod
    def load(cls, path: Path) -> ReviewConfig:
        try:
            raw = _RawConfig.model_validate(yaml.safe_load(path.read_text()) or {})
        except (OSError, ValidationError, yaml.YAMLError) as exc:
            raise ConfigError(f"invalid review config {path}: {exc}") from exc
        return cls._checked(raw, source=str(path))

    @classmethod
    def _checked(cls, raw: _RawConfig, *, source: str) -> ReviewConfig:
        for name in raw.accounts:
            if not re.fullmatch(NAME_PATTERN, name):
                raise ConfigError(
                    f"{source}: account name {name!r} must match {NAME_PATTERN} "
                    "(it names the account's review containers)"
                )
        logins: dict[str, str] = {}
        for name, account in raw.accounts.items():
            if isinstance(account, CodexAccount):
                login = str(account.credential.file)
                if login in logins:
                    raise ConfigError(
                        f"{source}: codex accounts {logins[login]!r} and {name!r} share the "
                        f"login {login}; its refresh token is single-use, so two refreshers "
                        "log it out"
                    )
                logins[login] = name

        repos: dict[str, RepoPolicy] = {}
        for name, repo in raw.repos.items():
            if not re.fullmatch(NAME_PATTERN, name):
                raise ConfigError(
                    f"{source}: repo key {name!r} must match {NAME_PATTERN} "
                    "(it names the repo's clone on disk)"
                )
            undeclared = [p for p in repo.providers if p not in raw.accounts]
            if undeclared:
                raise ConfigError(
                    f"{source}: repo {name!r} names undeclared account(s) "
                    f"{', '.join(undeclared)} (declared: {', '.join(sorted(raw.accounts))})"
                )
            repos[name] = repo.model_copy(update={"name": name, "project": name})
        listed = [r.repo for r in repos.values()]
        twice = sorted({r for r in listed if listed.count(r) > 1})
        if twice:
            raise ConfigError(f"{source}: a GitHub repo is listed twice: {', '.join(twice)}")
        return cls(
            instance=raw.instance,
            reviewer_bot=raw.reviewer_bot,
            github_app=raw.github_app,
            accounts=dict(raw.accounts),
            repos=repos,
            limits=raw.limits,
        )

    def enabled_repos(self) -> list[RepoPolicy]:
        return [repo for repo in self.repos.values() if repo.enabled]
