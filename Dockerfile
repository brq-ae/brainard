# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# --- runtime: the image actually shipped and run by compose ---
FROM base AS runtime

# Standard OCI metadata, so the published image is self-describing: registries
# and tools read these to show the source, license, and version of an image.
LABEL org.opencontainers.image.title="Brainard" \
      org.opencontainers.image.description="Self-hosted knowledge hub and coordination server for AI agents: shared doctrine, durable lessons, project handoffs, and live agent-to-agent rooms." \
      org.opencontainers.image.source="https://github.com/brq-ae/brainard" \
      org.opencontainers.image.url="https://github.com/brq-ae/brainard" \
      org.opencontainers.image.documentation="https://github.com/brq-ae/brainard#readme" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="1.0.0"

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]

# --- test: adds dev dependencies and the test suite; used only by the
# profile-gated `test` compose service, never part of the shipped image ---
FROM base AS test

COPY tests ./tests
# scripts/brain-wrapper.sh (ADR-0012 decision 12) has its own test coverage
# (tests/test_brain_wrapper.py) that drives the REAL script via subprocess
# against a throwaway local HTTP server -- never a modified copy -- so it
# needs to be present, byte-for-byte, in this image too. `curl` is what the
# script itself shells out to and is not part of the python:3.12-slim base.
COPY scripts ./scripts
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir .[dev]

CMD ["pytest", "-v"]
