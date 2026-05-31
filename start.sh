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

# Multi-line workspace_secrets (e.g. CODEX_AUTH_JSON, a JSON blob with
# embedded newlines) cannot ride docker's --env-file because Docker's
# parser would split the value into two attacker-controlled KEY=value
# records. molecule-controlplane writes them instead to
# /configs/secrets.d/<KEY> + a generated load.sh that exports each.
# Sourcing here makes the values visible to the boot-context snapshot
# below AND to every downstream consumer (CODEX_AUTH_BLOB resolution,
# auth.json materialization, codex_auth_refresh watchdog).
#
# Inert when the file is absent — older workspaces and other templates
# without multi-line secrets boot unchanged. Pre-2026-05-19 incident:
# CODEX_AUTH_JSON was silently dropped by the controlplane provisioner
# (newline filter in buildContainerizedUserData) → prod-Researcher
# 712b5600-d397-4749-adac-cd6ee574afea and prod-Reviewer
# 38f1c367-4e73-49a0-ad8b-6e2f8e7072c7 had no ~/.codex/auth.json and
# 401'd on every codex turn. Fix lands as molecule-controlplane PR
# pairing with this change.
if [ -f /configs/secrets.d/load.sh ]; then
  # shellcheck disable=SC1091
  . /configs/secrets.d/load.sh
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
for var in OPENAI_API_KEY MINIMAX_API_KEY KIMI_API_KEY CODEX_AUTH_JSON CODEX_CHATGPT_AUTH_JSON MOLECULE_ORG_ID; do
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

# Render ~/.codex/config.toml from the providers registry. Replaces
# the legacy codex_minimax_config.sh's hardcoded MiniMax path: the
# python helper reads `providers:` from config.yaml, resolves to the
# right provider against the current env (subscription preferred when
# CODEX_AUTH_JSON is set, then explicit model-prefix match, then
# credential auto-detect), and writes the model_provider block — or
# writes NOTHING when the picked provider is one of codex's built-in
# OpenAI auth modes (the verified prod shape for the subscription /
# OPENAI_API_KEY paths).
#
# The legacy codex_minimax_config.sh is kept as a compat fallback for
# this one release so external ops scripts and the existing test
# fixtures (which exec the .sh directly) keep working. Once those
# downstream consumers cut over, the .sh becomes purely historical.
PROVIDER_RENDER=""
for cand in /opt/adapter/render_provider_toml.py /usr/local/bin/render_provider_toml.py /app/render_provider_toml.py; do
  if [ -f "$cand" ]; then PROVIDER_RENDER="$cand"; break; fi
done
if [ -n "$PROVIDER_RENDER" ]; then
  # Prefer the runtime venv python (carries pyyaml). Falls back to
  # whichever python3 is on PATH; provider_config gracefully degrades
  # to its builtin registry if pyyaml is unavailable.
  PROVIDER_PY=""
  for cand in /opt/molecule-venv/bin/python3 /opt/molecule-venv/bin/python python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then PROVIDER_PY="$cand"; break; fi
  done
  if [ -n "$PROVIDER_PY" ]; then
    HOME=/home/agent CODEX_HOME=/home/agent/.codex \
      WORKSPACE_CONFIG_PATH="${WORKSPACE_CONFIG_PATH:-/configs}" \
      "$PROVIDER_PY" "$PROVIDER_RENDER" || \
      echo "[start.sh] WARN: render_provider_toml.py exited non-zero; falling back to legacy shell helper" >&2
  else
    echo "[start.sh] WARN: no python3 found; falling back to legacy codex_minimax_config.sh" >&2
    PROVIDER_RENDER=""
  fi
fi

# Legacy fallback path — kept so existing fixtures + ops scripts that
# call the .sh directly continue to work. When PROVIDER_RENDER ran
# successfully above this is a no-op (no MINIMAX_API_KEY or the
# python wrote the right config.toml already).
if [ -z "$PROVIDER_RENDER" ]; then
  if [ -f /usr/local/bin/codex_minimax_config.sh ]; then
    HOME=/home/agent CODEX_HOME=/home/agent/.codex \
      bash /usr/local/bin/codex_minimax_config.sh
  elif [ -f /app/codex_minimax_config.sh ]; then
    HOME=/home/agent CODEX_HOME=/home/agent/.codex \
      bash /app/codex_minimax_config.sh
  fi
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
# --- Mode C: headless ChatGPT/Codex-subscription auth (single-runner) ---
# Canonical credential: CODEX_AUTH_JSON. This is the CONTENTS of a
# codex `auth.json` (auth_mode:"chatgpt", OPENAI_API_KEY:null,
# tokens:{id_token,access_token,refresh_token,account_id},
# last_refresh) — the OpenClaw `openai-codex` provider's auth.order
# pattern (docs.openclaw.ai/providers/openai): prefer an injected
# subscription auth.json over a pay-as-you-go API key. The blob is
# stored in the self-hosted Infisical SSOT at secret path
# `/shared/codex-oauth`, key `CODEX_AUTH_JSON` (env=prod), and is
# injected into the workspace container as the CODEX_AUTH_JSON env
# var via the workspace Config-tab secret binding for EXACTLY ONE
# runner (the combined Reviewer+Researcher box on the CTO's
# ChatGPT/Codex subscription).
#
# CODEX_CHATGPT_AUTH_JSON is accepted as a DOCUMENTED backward-compat
# alias (the name shipped by template PR #5). CODEX_AUTH_JSON wins if
# both are set, so a Config-tab override can shadow a stale alias.
#
# Writing it to $CODEX_HOME/auth.json makes codex authenticate off the
# subscription instead of OPENAI_API_KEY. Codex's documented headless
# refresh (refresh-and-retry on 401, rewrites auth.json in place)
# handles token rotation; the persistent /home/agent volume keeps the
# refreshed file. We deliberately add NO refresh daemon — OpenAI's
# supported CI/CD pattern is "run codex and persist the updated
# auth.json", not a manual refresh endpoint (RFC §5).
#
# Inert when neither var is set: the OPENAI_API_KEY and MiniMax paths
# above are byte-unchanged and remain the DOCUMENTED FALLBACK. This is
# SINGLE-RUNNER only; there is intentionally no multi-workspace
# credential fanout (RFC §5, §8) — one auth.json per runner, never
# shared across concurrent jobs. The token is never echoed.
CODEX_AUTH_BLOB="${CODEX_AUTH_JSON:-${CODEX_CHATGPT_AUTH_JSON:-}}"
if [ -n "${CODEX_AUTH_BLOB}" ]; then
  CODEX_HOME_DIR="/home/agent/.codex"
  install -d -o agent -g agent "$CODEX_HOME_DIR"
  AUTH_JSON_PATH="${CODEX_HOME_DIR}/auth.json"
  # Write the injected contents verbatim. printf %s avoids any
  # interpretation of backslashes/format chars in the token blob.
  printf '%s' "${CODEX_AUTH_BLOB}" > "$AUTH_JSON_PATH"
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

# --- Codex auth RE-SYNC (GET-only) — codex shared-OAuth durable fix ---
# (2026-05-31) Multiple codex agents share ONE ChatGPT-Pro OAuth token
# (the platform global secret CODEX_AUTH_JSON). OpenAI's refresh_token is
# SINGLE-USE, so letting each per-agent app-server refresh on its own 401
# burned the shared seed within seconds. The durable fix:
#   - ONE owner rotates the refresh_token: the platform central refresher
#     (molecule-core internal/codexauth), which writes the rotated blob
#     back to global_secrets.
#   - EVERY workspace only RE-SYNCS (GET) the current token from the
#     platform and overwrites auth.json. codex_auth_sync.sh does this.
#
# CRITICAL ORDERING: the /home/agent volume PERSISTS auth.json across
# restarts. A STALE auth.json is exactly what triggers the 401→burn. So we
# run the sync `--once` SYNCHRONOUSLY HERE — BEFORE the codex app-server
# launches (it launches via `exec gosu agent molecule-runtime` at the end of
# this script) — so the app-server always starts on the platform's CURRENT
# token, never the stale persisted one. The ENV-injected auth.json above is
# only the bootstrap seed; the GET is authoritative.
SYNC_SCRIPT=""
for cand in /usr/local/bin/codex_auth_sync.sh /app/codex_auth_sync.sh; do
  if [ -x "$cand" ]; then SYNC_SCRIPT="$cand"; break; fi
done
if [ -n "$SYNC_SCRIPT" ]; then
  # Boot-time synchronous re-sync. rc 0=synced, 1=skip (not a shared-codex
  # workspace / no token), 2=transient. We log but NEVER fail start.sh — a
  # transient platform hiccup must not block boot; the loop + the bootstrap
  # ENV auth.json are the fallbacks.
  gosu agent env \
    CODEX_HOME=/home/agent/.codex \
    HOME=/home/agent \
    WORKSPACE_ID="${WORKSPACE_ID:-}" \
    PLATFORM_URL="${PLATFORM_URL:-}" \
    WORKSPACE_SERVER_URL="${WORKSPACE_SERVER_URL:-}" \
    CONFIGS_DIR="${CONFIGS_DIR:-}" \
    "$SYNC_SCRIPT" --once || \
    echo "[start.sh] codex_auth_sync --once non-zero (skip/transient); continuing — loop + bootstrap auth.json are the fallback" >&2
  # Long-running re-sync loop (background, gosu agent). Re-pulls the current
  # token every hour so a mid-session platform rotation propagates here.
  gosu agent env \
    CODEX_HOME=/home/agent/.codex \
    HOME=/home/agent \
    WORKSPACE_ID="${WORKSPACE_ID:-}" \
    PLATFORM_URL="${PLATFORM_URL:-}" \
    WORKSPACE_SERVER_URL="${WORKSPACE_SERVER_URL:-}" \
    CONFIGS_DIR="${CONFIGS_DIR:-}" \
    "$SYNC_SCRIPT" &
  echo "[start.sh] codex_auth_sync re-sync watchdog launched (pid=$!)"
else
  echo "[start.sh] WARN: codex_auth_sync.sh not found; codex auth re-sync disabled" >&2
fi

# --- OAuth refresh watchdog (RFC internal#569) — DISABLED BY DEFAULT ---
# The per-agent OAuth POST is gated off (CODEX_AUTH_REFRESH_OWNER!=1, which
# the template NEVER sets) by the durable shared-OAuth fix above: N agents
# sharing one refresh_token must NOT each rotate it. codex_auth_refresh.sh
# is retained (and still launched, so a single owner box can opt in via
# CODEX_AUTH_REFRESH_OWNER=1), but with the gate it only does GET-free skip
# logging. Token rotation is the platform central refresher's job; this
# workspace re-syncs via codex_auth_sync.sh above.
#
# (historical) Codex CLI 0.130.0 only refreshes auth.json on the demand path
# `AuthManager::auth().await`. In our prod-Reviewer / prod-Researcher
# topology the workspace can be idle (or wedged in executor.py) for
# >8 days between turns, so `auth()` never fires and the access_token
# silently expires. The watchdog below polls every 6h (default) and
# refreshes proactively via the same OAuth endpoint the CLI uses
# (https://auth.openai.com/oauth/token, client_id baked from
# codex-rs/login/src/auth/manager.rs). Inert when no auth.json is
# present OR when auth_mode != chatgpt (the API-key / MiniMax paths
# don't have refresh tokens). The watchdog is started ONLY when an
# auth.json was actually materialized above — for plain OPENAI_API_KEY
# or MINIMAX_API_KEY workspaces it doesn't run.
#
# Started under gosu agent so it inherits the same uid as molecule-
# runtime and can read/write the same agent-owned auth.json. PID is
# the start.sh shell's child; when the container is stopped or
# restarted the watchdog exits with it (no orphan).
if [ -s "/home/agent/.codex/auth.json" ]; then
  WATCHDOG=""
  for cand in /usr/local/bin/codex_auth_refresh.sh /app/codex_auth_refresh.sh; do
    if [ -x "$cand" ]; then WATCHDOG="$cand"; break; fi
  done
  if [ -n "$WATCHDOG" ]; then
    # Boot-time priming probe — runs the staleness check once
    # synchronously so the container starts with a known-good token
    # window. `--once` returns 0 (refreshed), 1 (skipped because
    # already fresh), or 2/3 (failure). We log but never fail
    # start.sh on a refresh problem — the CLI's own demand-path
    # refresh is still the durable safety net.
    gosu agent env \
      CODEX_HOME=/home/agent/.codex \
      HOME=/home/agent \
      "$WATCHDOG" --once || \
      echo "[start.sh] WARN: codex_auth_refresh --once non-zero; continuing — watchdog loop will retry" >&2
    # Long-running loop (background, gosu agent). Output goes to
    # docker logs alongside molecule-runtime.
    gosu agent env \
      CODEX_HOME=/home/agent/.codex \
      HOME=/home/agent \
      "$WATCHDOG" &
    echo "[start.sh] codex_auth_refresh watchdog launched (pid=$!)"
  else
    echo "[start.sh] WARN: codex_auth_refresh.sh not found; OAuth proactive refresh disabled (RFC internal#569)" >&2
  fi
fi

# Hand off to molecule-runtime. From here, every A2A message routes
# through executor.py → app_server.py → codex app-server child.
exec gosu agent molecule-runtime
