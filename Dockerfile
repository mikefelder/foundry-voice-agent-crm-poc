# syntax=docker/dockerfile:1

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

# The azure extra is needed by the browser relay, which connects to Voice Live.
RUN pip install --no-cache-dir ".[api,azure]"

# The recorded fixture ships in the image so CRM_PROVIDER=fake needs no network.
ENV CRM_PROVIDER=fake \
    PORT=8000

# Non-root: the app needs no write access to anything in the image.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

CMD ["sh", "-c", "uvicorn crm_companion.api.app:create_app --factory --host 0.0.0.0 --port ${PORT}"]
