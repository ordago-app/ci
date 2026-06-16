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

CMD ["python", "-m", "src.main"]
