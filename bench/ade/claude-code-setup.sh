#!/bin/bash
# Installs the plain-agent baseline's CLI in an ADE-bench task container. Replaces
# ADE-bench's own setup script only to pin the version (bench/agents.py,
# CLAUDE_CODE_VERSION); it is sourced in the task's shell, so no `set -e`.
echo "Setup Claude Code"
node --version
npm --version
npm install -g @anthropic-ai/claude-code@2.1.328
claude --version
echo "Installed Claude Code"
