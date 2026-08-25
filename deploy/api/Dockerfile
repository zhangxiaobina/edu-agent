# syntax=docker/dockerfile:1.7
# Base images are explicit, multi-architecture registry digests. Release
# builds should update them only through a reviewed dependency change.
ARG PYTHON_IMAGE=python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /build

# Resolve only from the committed lockfile before copying application source.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY edu_agent ./edu_agent
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=random \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    PYTHONPATH=/app \
    EDU_AGENT_CONFIG=/etc/edu-agent/config.toml \
    EDU_AGENT_API_HOST=127.0.0.1 \
    EDU_AGENT_API_PORT=8080

RUN groupadd --system --gid 10001 eduagent \
    && useradd --system --uid 10001 --gid 10001 --home-dir /tmp --no-create-home eduagent \
    && install -d -o 10001 -g 10001 -m 0700 /var/lib/edu-agent/state /var/lib/edu-agent/artifacts \
    && install -d -o 10001 -g 10001 -m 0750 /etc/edu-agent /app /tmp/.cache

COPY --from=builder /opt/venv /opt/venv
COPY edu_agent /app/edu_agent
COPY scripts/api_server.py scripts/container_preflight.py scripts/state_maintenance.py /app/scripts/
COPY deploy/api/entrypoint.sh /usr/local/bin/edu-agent-entrypoint
COPY deploy/api/config.container.example.toml /etc/edu-agent/config.example.toml
RUN chmod 0555 /usr/local/bin/edu-agent-entrypoint \
    && chown -R root:root /app /opt/venv /usr/local/bin/edu-agent-entrypoint /etc/edu-agent/config.example.toml \
    && chmod 0444 /etc/edu-agent/config.example.toml

USER 10001:10001
WORKDIR /app
ENTRYPOINT ["/usr/local/bin/edu-agent-entrypoint"]
