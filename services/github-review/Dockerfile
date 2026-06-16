FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git curl jq \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
RUN pip install --no-cache-dir -e .

# Authenticate git fetches against private repos as the reviewer GitHub App.
# The helper asks claude-router for a role-scoped token (AGENT_ROLE=reviewer);
# the token never lands in argv or logs. Mirrors codex-code's agent overlay.
COPY bin/git-credential-helper /usr/local/bin/git-credential-helper
RUN chmod +x /usr/local/bin/git-credential-helper \
 && git config --system credential.helper /usr/local/bin/git-credential-helper \
 && git config --system credential.useHttpPath false

CMD ["python", "-m", "src.main"]
