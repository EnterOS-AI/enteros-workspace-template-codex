#!/usr/bin/env bash
# codex_minimax_config.sh — write the official MiniMax provider config
# into ~/.codex/config.toml when MINIMAX_API_KEY is set. Sourced by
# start.sh (Docker boot) and the bare-host install path so both get
# the same shape.
#
# Source: https://platform.minimax.io/docs/token-plan/codex-cli
#
# WIRE_API CONTRACT (codex CLI 0.130, baked by #219 — internal#513):
# codex CLI 0.130 REMOVED the `chat` WireApi variant entirely (the
# OpenAI Chat Completions wire was dropped fleet-wide in Feb 2026,
# https://github.com/openai/codex/discussions/7782). 0.130 hard-fails
# config.toml parsing with
#   `wire_api = "chat"` is no longer supported. set `wire_api = "responses"`
# at the line that holds it — BEFORE auth.json / OPENAI_API_KEY is even
# read. So `chat` is invalid UNCONDITIONALLY for every provider block
# under 0.130; it is no longer a per-provider choice. We therefore emit
# `wire_api = "responses"` (the only parse-valid value on this CLI).
#
# PROD IMPACT: the production codex agents (Reviewer/Researcher) run the
# ChatGPT/Codex *subscription* OAuth path (CODEX_AUTH_JSON → ~/.codex/
# auth.json, model gpt-5.5). That is the OpenAI provider, which natively
# requires the Responses API on 0.130 — so `responses` is exactly
# correct for the path prod actually uses.
#
# KNOWN MINIMAX LIMITATION (documented, out of scope for internal#513):
# MiniMax's token-plan endpoint (https://api.minimax.io/v1) is an
# OpenAI *Chat Completions*-compatible API; per MiniMax's own codex-cli
# docs it does NOT serve the OpenAI Responses API. With `chat` removed
# from the CLI there is no parse-valid wire that MiniMax natively
# accepts on 0.130 — the MiniMax token-plan route needs its own
# follow-up (a Responses→Chat translation shim, or MiniMax shipping
# Responses support). We still write `responses` here so the config
# PARSES (the alternative is a non-loadable config that bricks every
# boot, incl. the subscription path). This is acceptable because no
# prod agent uses the MiniMax leg today (subscription is `Preferred`
# per README; OPENAI_API_KEY is the documented fallback) — both of
# those are native Responses providers. Tracked: molecule-ai/internal#514
# ("codex MiniMax token-plan leg incompatible with CLI 0.130 Responses-API,
# deferred from #513") — the dedicated follow-up for the MiniMax-Responses
# gap; internal#513 remains this change's cutover-blocker.
#
# When MINIMAX_API_KEY is missing this is a no-op so OpenAI-direct
# users see no behavior change.
#
# SUBSCRIPTION TAKES PRECEDENCE OVER THE MINIMAX ALT (internal#513):
# When the ChatGPT/Codex subscription is injected (CODEX_AUTH_JSON or
# its CODEX_CHATGPT_AUTH_JSON backward-compat alias is set — the prod
# Reviewer/Researcher path, #219) this script MUST NOT write the
# minimax provider block, even if MINIMAX_API_KEY also happens to be
# present on the same workspace. Codex 0.130's built-in OpenAI
# provider IS the subscription provider: with auth.json
# (auth_mode:"chatgpt") and NO `model_provider` override in
# config.toml, codex routes to the OpenAI/Codex Responses backend
# natively (verified: a working device-logged codex-0.130 config.toml
# carries NO model_provider / model / base_url / wire_api block at
# all — the model is selected via thread/start, which the adapter
# passes as gpt-5.5 from config.yaml). If we instead emit the minimax
# block here, config.toml pins model_provider=minimax +
# base_url=https://api.minimax.io/v1 while start.sh's mode-C only
# appends auth keys (it does NOT rewrite the provider) — so codex
# authenticates off the subscription but POSTs to
# https://api.minimax.io/v1/responses, which MiniMax does not serve
# → "unexpected status 404 Not Found ... url:
# https://api.minimax.io/v1/responses" on EVERY turn. That is the
# exact live A2A blocker observed on prod-Reviewer
# (469b511b-5794-4847-9da0-ddc0a9e6bc24) and prod-Researcher
# (4dfbd391-b541-437d-852c-88d80c3ffadc). The PR#10 wire_api flip was
# necessary (config parses) but NOT sufficient (still wrong provider).
# The minimax leg's own Chat-vs-Responses incompatibility on CLI 0.130
# stays tracked separately as molecule-ai/internal#514 and is NOT
# regressed here — it just no longer shadows the prod subscription
# path. Skip is a true no-op (identical to the no-MINIMAX_API_KEY
# branch): config.toml is left without a provider override so codex
# uses its built-in subscription provider.

set -euo pipefail

if [ -n "${CODEX_AUTH_JSON:-${CODEX_CHATGPT_AUTH_JSON:-}}" ]; then
  echo "[codex-minimax] ChatGPT/Codex subscription present (CODEX_AUTH_JSON" \
    "/ alias) — skipping minimax provider block so codex uses its" \
    "built-in subscription provider (Responses API, model via" \
    "thread/start). The MiniMax alt is subordinate to the" \
    "subscription (internal#513; MiniMax-Responses gap = internal#514)."
  return 0 2>/dev/null || exit 0
fi

if [ -z "${MINIMAX_API_KEY:-}" ]; then
  echo "[codex-minimax] no MINIMAX_API_KEY — codex will use its default (OpenAI) provider"
  return 0 2>/dev/null || exit 0
fi

CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
mkdir -p "$CODEX_HOME"
CONFIG_TOML="${CODEX_HOME}/config.toml"

# Defaults pulled from MiniMax's official Codex CLI config
# (https://platform.minimax.io/docs/token-plan/codex-cli). Operators
# can override the model via CODEX_MINIMAX_MODEL or the base_url via
# MINIMAX_API_BASE (mainland-China ingress is api.minimaxi.com/v1).
MODEL="${CODEX_MINIMAX_MODEL:-codex-MiniMax-M2.7}"
BASE_URL="${MINIMAX_API_BASE:-https://api.minimax.io/v1}"

cat > "$CONFIG_TOML" <<EOF
# Auto-generated by codex_minimax_config.sh — do not edit by hand;
# regenerated on every boot from MINIMAX_API_KEY / CODEX_MINIMAX_MODEL
# / MINIMAX_API_BASE env. Source: MiniMax docs (token-plan/codex-cli).
model = "${MODEL}"
model_provider = "minimax"

[model_providers.minimax]
name = "MiniMax Chat Completions API"
base_url = "${BASE_URL}"
env_key = "MINIMAX_API_KEY"
# codex CLI 0.130 removed the `chat` WireApi variant — `responses` is
# the only parse-valid value (internal#513). See header note for the
# known MiniMax-Responses limitation; this keeps config.toml loadable
# so the subscription/OpenAI paths boot. DO NOT revert to "chat": CLI
# 0.130 hard-fails config parse at this exact line and the codex agent
# loop never starts (the live A2A blocker on prod-Reviewer/Researcher).
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 4
stream_max_retries = 10
stream_idle_timeout_ms = 300000
EOF

# Inherit ownership from the codex home dir so the agent user (which
# runs molecule-runtime) can read it under gosu.
if command -v stat >/dev/null 2>&1; then
  owner=$(stat -c "%u:%g" "$CODEX_HOME" 2>/dev/null || echo "")
  if [ -n "$owner" ]; then
    chown "$owner" "$CONFIG_TOML" 2>/dev/null || true
  fi
fi

echo "[codex-minimax] wrote ${CONFIG_TOML} model=${MODEL} provider=minimax"

# Also patch /configs/config.yaml so molecule-runtime's load_config()
# passes ${MODEL} to executor.py instead of the library default
# (anthropic:claude-opus-4-7), which codex CLI rejects with
# `unknown model 'anthropic:claude-opus-4-7' (2013)` and hangs the
# first turn until task_complete that never arrives. Caught live on
# the 4-runtime A2A E2E (2026-05-03): codex executor took the turn
# lock, called turn/start with the wrong model, and never released
# — every subsequent A2A request piled up in the workspace-server
# queue as "busy".
#
# Some workspace configurations omit the resolved model while retaining
# provider context, so this boot-time helper patches config.yaml from the
# environment that already knows about MINIMAX_API_KEY.
WORKSPACE_CONFIG_DIR="${WORKSPACE_CONFIG_PATH:-/configs}"
WORKSPACE_CONFIG="${WORKSPACE_CONFIG_DIR}/config.yaml"
if [ -f "$WORKSPACE_CONFIG" ] && [ -w "$WORKSPACE_CONFIG_DIR" ]; then
  if grep -qE '^model:' "$WORKSPACE_CONFIG"; then
    sed -i.bak "s|^model: .*|model: '${MODEL}'|" "$WORKSPACE_CONFIG" && rm -f "${WORKSPACE_CONFIG}.bak"
  else
    printf "model: '%s'\n" "$MODEL" >> "$WORKSPACE_CONFIG"
  fi
  echo "[codex-minimax] patched ${WORKSPACE_CONFIG} model=${MODEL}"
elif [ -f "$WORKSPACE_CONFIG" ]; then
  # Read-only mount or wrong owner — log so operators can debug
  # rather than silently shipping the wrong default model.
  echo "[codex-minimax] WARN: ${WORKSPACE_CONFIG} exists but ${WORKSPACE_CONFIG_DIR} not writable; runtime will use default model" >&2
fi
