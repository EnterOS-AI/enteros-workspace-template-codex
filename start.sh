#!/usr/bin/env bash
# Boot script for the codex workspace template.
#
# Unlike hermes (which boots a separate gateway daemon on :8642 first),
# codex's app-server is a stdio child of executor.py — there's no
# network service to start, no port to wait on, no health endpoint.
# This script just verifies the binary is installed and exec's
# molecule-runtime.

set -euo pipefail

# --- Make /configs agent-owned (fleet contract) ---
# T4 atomic-co-sequencing contract (RFC internal#456 §10): the T4
# escalation leg (sudo NOPASSWD + docker group, baked in the
# Dockerfile) is ADDITIVE — the agent still runs uid-1000 (the
# `exec gosu agent` below is UNCHANGED). The token MUST stay
# agent-readable so the runtime AND the codex MCP child both resolve
# the SAME .auth_token; escalation must NOT regress the codex
# list_peers-401 token-resolution class.
#
# The /configs volume is created by Docker/the provisioner as root.
# molecule_runtime/configs_dir.py picks /configs only when it exists
# AND is agent-writable, else falls back to $HOME/.molecule-workspace.
# Without this chown, /configs is root-owned, the runtime falls back
# to ~/.molecule-workspace for .auth_token — but codex_mcp_config.sh
# (pre-fix) hard-pinned CONFIGS_DIR=/configs into the MCP env, so the
# MCP child looked at the empty root-owned /configs and every
# list_peers/delegate_task 401'd ("No peers found") while the runtime
# itself was fully authed. This chown makes /configs the single
# agent-owned resolution point for BOTH; codex_mcp_config.sh's
# resolve()-based CONFIGS_DIR is the belt-and-suspenders half so they
# can never diverge again. Mirrors claude-code entrypoint.sh (12dd604)
# + hermes start.sh (PR#24/#26) `chown -R agent:agent /configs`.
# Runs as root here (before any gosu), so it takes effect for the
# agent-context children below.
if [ "$(id -u)" = "0" ]; then
  chown -R agent:agent /configs 2>/dev/null || true
fi

# Boot-context snapshot — emitted on EVERY container start. Lets
# `docker logs` answer "what env / uid was actually present?" without
# docker exec into a possibly-dying container. Logs NAMES of
# auth-relevant env vars, never VALUES. Mirrors the claude-code /
# hermes entrypoint boot-context block.
echo "----- start.sh boot $(date -u +%Y-%m-%dT%H:%M:%SZ) -----"
echo "uid=$(id -u) gid=$(id -g) user=$(id -un 2>/dev/null || echo unknown)"
echo "workspace_id=${WORKSPACE_ID:-<unset>} platform_url=${PLATFORM_URL:-<unset>}"
echo "configs_dir: $(ls -ld /configs 2>/dev/null || echo MISSING)"
for var in OPENAI_API_KEY MINIMAX_API_KEY KIMI_API_KEY MOLECULE_ORG_ID; do
  eval "val=\${$var:-}"
  if [ -n "${val:-}" ]; then echo "env $var=set"; else echo "env $var=unset"; fi
done
echo "------------------------------------------------"

# Fail-fast preflight: codex binary must be on PATH. The Dockerfile
# installs @openai/codex globally; if it isn't here, something's wrong
# with the image build.
if ! command -v codex >/dev/null 2>&1; then
  echo "[start.sh] FATAL: codex binary not on PATH. Image misbuilt?" >&2
  exit 1
fi

CODEX_VERSION="$(codex --version 2>&1 || echo unknown)"
echo "[start.sh] codex installed: ${CODEX_VERSION}"

# Pre-create ~/.codex so codex doesn't try to mkdir it on first run as
# the wrong user. Persistent volume mount goes here for thread state.
install -d -o agent -g agent /home/agent/.codex
install -d -o agent -g agent /home/agent/.codex/sessions

# Generate the MiniMax provider config.toml when MINIMAX_API_KEY is
# present. No-op when the operator is using OpenAI direct (the env
# the codex CLI checks defaults to OPENAI_API_KEY in that path).
# Source: https://platform.minimax.io/docs/token-plan/codex-cli
if [ -f /usr/local/bin/codex_minimax_config.sh ]; then
  HOME=/home/agent CODEX_HOME=/home/agent/.codex \
    bash /usr/local/bin/codex_minimax_config.sh
elif [ -f /app/codex_minimax_config.sh ]; then
  HOME=/home/agent CODEX_HOME=/home/agent/.codex \
    bash /app/codex_minimax_config.sh
fi

# Append the molecule A2A MCP server block — gives the codex agent
# list_peers / delegate_task / commit_memory etc. as MCP tools (same
# capability claude-code's mcp_servers["a2a"] wiring provides). Order
# matters: codex_minimax_config.sh writes config.toml with `cat >`
# (overwrite); this one uses `cat >>` (append) and so must run after.
# Tracks issue molecule-ai-workspace-template-codex#15.
if [ -f /usr/local/bin/codex_mcp_config.sh ]; then
  HOME=/home/agent CODEX_HOME=/home/agent/.codex \
    bash /usr/local/bin/codex_mcp_config.sh
elif [ -f /app/codex_mcp_config.sh ]; then
  HOME=/home/agent CODEX_HOME=/home/agent/.codex \
    bash /app/codex_mcp_config.sh
fi
# --- Mode C: headless ChatGPT-subscription auth (single-runner only) ---
# When CODEX_CHATGPT_AUTH_JSON is set (the CONTENTS of a codex
# `auth.json`, auth_mode:chatgpt, injected via the workspace Config tab
# secret for EXACTLY ONE runner — the future combined Reviewer+
# Researcher box on the CTO's ChatGPT subscription), write it to
# $CODEX_HOME/auth.json so codex authenticates off the subscription
# instead of OPENAI_API_KEY. Codex's documented headless refresh
# (refresh-and-retry on 401, rewrites auth.json in place) handles token
# rotation; the persistent /home/agent volume keeps the refreshed file.
# We deliberately add NO refresh daemon — OpenAI's supported CI/CD
# pattern is "run codex and persist the updated auth.json", not a
# manual refresh endpoint (RFC §5).
#
# Inert when CODEX_CHATGPT_AUTH_JSON is unset: the OPENAI_API_KEY and
# MiniMax paths above are byte-unchanged. This is SINGLE-RUNNER only;
# there is intentionally no multi-workspace credential fanout (RFC §5,
# §8) — one auth.json per runner, never shared across concurrent jobs.
if [ -n "${CODEX_CHATGPT_AUTH_JSON:-}" ]; then
  CODEX_HOME_DIR="/home/agent/.codex"
  install -d -o agent -g agent "$CODEX_HOME_DIR"
  AUTH_JSON_PATH="${CODEX_HOME_DIR}/auth.json"
  # Write the injected contents verbatim. printf %s avoids any
  # interpretation of backslashes/format chars in the token blob.
  printf '%s' "${CODEX_CHATGPT_AUTH_JSON}" > "$AUTH_JSON_PATH"
  chown agent:agent "$AUTH_JSON_PATH"
  chmod 0600 "$AUTH_JSON_PATH"
  # Ensure codex reads file-backed credentials (not the OS keyring,
  # which is absent in the container). Append to config.toml only if
  # the key isn't already present so we don't fight the minimax helper.
  CONFIG_TOML="${CODEX_HOME_DIR}/config.toml"
  touch "$CONFIG_TOML"
  if ! grep -qE '^[[:space:]]*cli_auth_credentials_store[[:space:]]*=' "$CONFIG_TOML"; then
    printf 'cli_auth_credentials_store = "file"\n' >> "$CONFIG_TOML"
  fi
  if ! grep -qE '^[[:space:]]*forced_login_method[[:space:]]*=' "$CONFIG_TOML"; then
    printf 'forced_login_method = "chatgpt"\n' >> "$CONFIG_TOML"
  fi
  chown agent:agent "$CONFIG_TOML"
  echo "[start.sh] chatgpt-auth: wrote ${AUTH_JSON_PATH} (0600 agent) + config.toml file-store keys (single-runner)"
fi

# Reapply ownership in case the helpers wrote into agent's home as root.
chown -R agent:agent /home/agent/.codex 2>/dev/null || true

# Provider preflight: at least one of OPENAI_API_KEY, MINIMAX_API_KEY,
# or an injected ChatGPT-subscription auth.json must be present. The
# adapter's setup() also checks but surfacing here gives operators a
# clearer signal in container logs.
if [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${MINIMAX_API_KEY:-}" ] \
   && [ ! -s "/home/agent/.codex/auth.json" ]; then
  echo "[start.sh] WARN: no OPENAI_API_KEY, MINIMAX_API_KEY, nor ~/.codex/auth.json. Workspace will fail preflight." >&2
fi

# Hand off to molecule-runtime. From here, every A2A message routes
# through executor.py → app_server.py → codex app-server child.
exec gosu agent molecule-runtime
