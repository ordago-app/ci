FROM python:3.12-slim

WORKDIR /app

# Install runtime deps from pyproject (without dev extras) via uv for a fast, locked build.
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv export --no-dev --frozen --no-emit-project > requirements.txt \
    && pip install --no-cache-dir -r requirements.txt

COPY src ./src

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "src.main"]
