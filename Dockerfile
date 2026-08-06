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

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]

# --- test: adds dev dependencies and the test suite; used only by the
# profile-gated `test` compose service, never part of the shipped image ---
FROM base AS test

COPY tests ./tests
RUN pip install --no-cache-dir .[dev]

CMD ["pytest", "-v"]
