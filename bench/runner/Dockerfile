# Linux runner for the benchmarks' own harnesses on a Windows host.
#
# ADE-bench and Harbor (data-eng-bench) both build container and mount paths with the
# host's path rules, which break on Windows ("\tmp", "C:\..." mount targets). This image
# runs them in Linux and drives the host's Docker through the mounted socket. bench/run.py
# mounts ~/.verimend at the path Docker Desktop itself uses for host files
# (/run/desktop/mnt/host/c/...), so every bind mount the harness asks for resolves the
# same in each container. Promoted from scripts/spikes/ade_runner/ (phase 1).
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
