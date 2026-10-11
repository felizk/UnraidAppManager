# syntax=docker/dockerfile:1.7
# Unraid App Manager. Runs as root: it needs the Docker socket and writes Unraid templates
# to the FAT-formatted flash drive, where ownership can't be changed.
#
# Behind a TLS-intercepting proxy, pass its CA bundle as a build secret:
#   docker build --secret id=ca_bundle,src=/path/to/ca.crt ...

ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=secret,id=ca_bundle,required=false \
    --mount=type=cache,target=/root/.cache \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle PIP_CERT=/run/secrets/ca_bundle; fi; \
    pip install --root-user-action=ignore uv==0.11.32 \
    && uv sync --locked --no-dev --no-install-project
COPY uam ./uam
COPY README.md ./
RUN --mount=type=secret,id=ca_bundle,required=false \
    --mount=type=cache,target=/root/.cache \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    uv sync --locked --no-dev --no-editable --reinstall-package unraid-app-manager

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="Unraid App Manager" \
      org.opencontainers.image.description="Install and update Unraid apps straight from GitHub repositories"
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    UAM_DATA_DIR=/data \
    UAM_PORT=8484
VOLUME /data
EXPOSE 8484
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"UAM_PORT\",\"8484\")}/api/health', timeout=4)" || exit 1
CMD ["python", "-m", "uam"]
