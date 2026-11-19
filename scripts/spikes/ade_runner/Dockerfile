# Linux runner for ADE-bench's harness on a Windows host.
#
# ADE-bench builds container paths with pathlib, which gives "\tmp" on Windows, so its
# harness cannot run natively there. This image runs the harness in Linux and talks to
# the host's Docker through the mounted socket. See scripts/spikes/ade_run.ps1.
FROM python:3.12-slim

COPY --from=docker:28-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:28-cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins

RUN apt-get update \
    && apt-get install -y --no-install-recommends git tmux \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv==0.13.7"

ENV UV_LINK_MODE=copy \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    USE_DYNAMIC_LOGGING=FALSE \
    PYTHONIOENCODING=utf-8
