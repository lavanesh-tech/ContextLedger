# syntax=docker/dockerfile:1.7

# Base images are pinned to a Debian release; Dependabot (.github/dependabot.yml)
# proposes updates, and CI scans every built image with Trivy.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---- build stage: locked dependencies first (cached layer), then the app -----
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
# Only the lock file: this layer is rebuilt when dependencies change, not when code does.
# --require-hashes: every downloaded file must match the hash in the lock file.
COPY requirements.lock ./
RUN pip install --require-hashes --no-deps -r requirements.lock

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-deps .

# ---- runtime stage: no build tools, no source tree, non-root user -----------
FROM ${PYTHON_IMAGE} AS runtime

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="contextledger-api" \
      org.opencontainers.image.description="MCP-native temporal RAG and decision-provenance API" \
      org.opencontainers.image.source="https://github.com/lavanesh-tech/ContextLedger" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
# Migrations ship with the image so the same artifact can run
# `alembic upgrade head` as a one-off deploy step (root-owned, read-only).
COPY alembic.ini ./
COPY migrations ./migrations
USER 10001:10001
EXPOSE 8000
# The app writes nothing to disk: the container can run with a read-only root filesystem.
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2).status == 200 else 1)"]

# --proxy-headers only trusts X-Forwarded-* from FORWARDED_ALLOW_IPS (default 127.0.0.1);
# set it to the load balancer's range when deployed behind one.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers", "--timeout-graceful-shutdown", "20"]
