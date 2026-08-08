FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git curl jq \
 && rm -rf /var/lib/apt/lists/*

# Deps come from the committed uv.lock, so a rebuild installs the exact versions
# that were tested. `pip install -e .` ignored the lock, which let a rebuild pull
# an untested major version (that is what broke a-private-homelab-service on 2026-08-07).
COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy

WORKDIR /app

# --no-install-project: this service has no build backend and is run as
# `python -m src.main` from /app, so only the dependencies need installing.
# Keeping src out of this layer also keeps it cached until uv.lock changes.
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project

COPY src /app/src

ENV PATH="/app/.venv/bin:$PATH"

# Authenticate git fetches against private repos as the reviewer GitHub App.
# The helper asks claude-router for a role-scoped token (AGENT_ROLE=reviewer);
# the token never lands in argv or logs. Mirrors codex-code's agent overlay.
COPY bin/git-credential-helper /usr/local/bin/git-credential-helper
RUN chmod +x /usr/local/bin/git-credential-helper \
 && git config --system credential.helper /usr/local/bin/git-credential-helper \
 && git config --system credential.useHttpPath false

CMD ["python", "-m", "src.main"]
