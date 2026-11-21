# The plain-agent baseline's container for the fault suite (bench/faults/container.py).
# Built with no context: `docker build - < Dockerfile`. Versions come in as build args,
# read from this repository's own environment, so dbt and DuckDB match the host that
# built the fault's database, and the Claude Code CLI is the pinned one.
FROM python:3.12-slim

ARG DBT_CORE_VERSION
ARG DBT_DUCKDB_VERSION
ARG DUCKDB_VERSION
ARG CLAUDE_CODE_VERSION

RUN apt-get update \
    && apt-get install -y --no-install-recommends git nodejs npm \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "dbt-core==${DBT_CORE_VERSION}" \
    "dbt-duckdb==${DBT_DUCKDB_VERSION}" \
    "duckdb==${DUCKDB_VERSION}"

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" && claude --version

# The project is a bind mount from another machine's file system: trust it, and ignore
# file modes and line-ending rewrites, so `git status` shows only what the agent changed.
RUN git config --system --add safe.directory '*' \
    && git config --system core.fileMode false \
    && git config --system core.autocrlf false

ENV DBT_PROFILES_DIR=/work/profiles \
    DBT_SEND_ANONYMOUS_USAGE_STATS=False

WORKDIR /work/project
