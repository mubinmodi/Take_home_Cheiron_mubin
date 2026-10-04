# syntax=docker/dockerfile:1
# The hosted service (docs/hosted-deployment.md): one process with one worker per container, because
# the per-process state (circuit breakers, caches) is per worker. The platform may set PORT (default 8080).

FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.6.16 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# Dependencies first: this layer is reused until uv.lock changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-bookworm
# vl-convert draws chart text with system fonts, and slim images have none.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-liberation2 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 app
COPY --from=build --chown=app:app /app/.venv /app/.venv
USER app
WORKDIR /home/app
# FORWARDED_ALLOW_IPS: the load balancer sets X-Forwarded-Proto; trusting it makes chart links https.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DEPLOYMENT=hosted \
    PORT=8080 \
    FORWARDED_ALLOW_IPS="*"
EXPOSE 8080
CMD ["clinical-trials-viz", "serve", "--host", "0.0.0.0"]
