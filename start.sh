#!/usr/bin/env bash
# Boot script for the codex workspace template.
#
# Unlike hermes (which boots a separate gateway daemon on :8642 first),
# codex's app-server is a stdio child of executor.py — there's no
# network service to start, no port to wait on, no health endpoint.
# This script just verifies the binary is installed and exec's
# molecule-runtime.

set -euo pipefail

# Fail-fast preflight: codex binary must be on PATH. The Dockerfile
# installs @openai/codex globally; if it isn't here, something's wrong
# with the image build.
if ! command -v codex >/dev/null 2>&1; then
  echo "[start.sh] FATAL: codex binary not on PATH. Image misbuilt?" >&2
  exit 1
fi

CODEX_VERSION="$(codex --version 2>&1 || echo unknown)"
echo "[start.sh] codex installed: ${CODEX_VERSION}"

# OPENAI_API_KEY must be set (codex reads it from env). The adapter's
# setup() also checks this and fails the workspace at preflight, but
# surfacing it here gives operators a clearer signal in container logs.
if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "[start.sh] WARN: OPENAI_API_KEY not set. Workspace will fail preflight." >&2
fi

# Pre-create ~/.codex so codex doesn't try to mkdir it on first run as
# the wrong user. Persistent volume mount goes here for thread state.
install -d -o agent -g agent /home/agent/.codex
install -d -o agent -g agent /home/agent/.codex/sessions

# Hand off to molecule-runtime. From here, every A2A message routes
# through executor.py → app_server.py → codex app-server child.
exec gosu agent molecule-runtime
