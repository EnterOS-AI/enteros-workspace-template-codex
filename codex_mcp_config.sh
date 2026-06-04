#!/usr/bin/env bash
# codex_mcp_config.sh — append the molecule A2A MCP server block to
# ~/.codex/config.toml so the codex CLI can call list_peers /
# delegate_task / commit_memory / recall_memory / send_message_to_user
# / get_workspace_info / check_task_status as MCP tools.
#
# This is the codex equivalent of how claude_sdk_executor.py wires
# `mcp_servers["a2a"]` into the claude-agent-sdk options for the
# claude-code template. Without it, every codex workspace boots blind
# to its peers and reports "list_peers not available" the first time
# the agent tries to coordinate. Tracks issue
# Molecule-AI/molecule-ai-workspace-template-codex#15.
#
# Schema reference (codex-rs rust-v0.57.0 docs/config.md, MCP section):
#   [mcp_servers.<name>]
#   command = "<binary>"
#   args = ["..."]
#   env = { "KEY" = "value" }   # extra env merged with codex's whitelist
#   env_vars = ["EXTRA_FORWARD"] # additional env-passthrough names
#
# Codex's default env whitelist (codex-rs/rmcp-client/src/utils.rs)
# covers HOME / PATH / LANG / TERM / TMPDIR / etc. but NOT the molecule-
# specific runtime env (WORKSPACE_ID, PLATFORM_URL, MOLECULE_ORG_ID,
# CONFIGS_DIR). We resolve them at install time and write literal
# values into the env map so the MCP child process has them regardless
# of how codex spawns it.
#
# Composition with codex_minimax_config.sh: that script uses `cat >`
# (overwrite). This script uses `cat >>` (append) and MUST run AFTER
# the minimax script — install.sh + start.sh both invoke them in that
# order. Idempotent: re-running on an already-configured config.toml
# strips the previous [mcp_servers.molecule] block before re-appending,
# so reboots don't accumulate duplicate entries.

set -euo pipefail

CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
mkdir -p "$CODEX_HOME"
CONFIG_TOML="${CODEX_HOME}/config.toml"

# Resolve python interpreter at install time. Sandbox-spawned MCP
# children may not inherit the same PATH as the parent shell, so the
# absolute path is safer than relying on `python3` being resolvable
# inside whatever sandbox codex spawns the child under.
#
# Prefer the runtime venv (`/opt/molecule-venv/bin/python3`) where
# molecule-ai-workspace-runtime is installed by the host install.sh.
# /usr/bin/python3 has only the system stdlib and no molecule_runtime,
# so picking it here causes codex to fail the MCP handshake on first
# call: "MCP client for `molecule` failed to start: handshaking with
# MCP server failed: connection closed: initialize response" (the
# subprocess crashes with ModuleNotFoundError before the JSON-RPC
# handshake completes). Walk a list of well-known interpreters and
# pick the first one that can `import molecule_runtime`.
resolve_python() {
  if [ -n "${MOLECULE_MCP_PYTHON:-}" ] && [ -x "${MOLECULE_MCP_PYTHON}" ]; then
    echo "${MOLECULE_MCP_PYTHON}"
    return
  fi
  for cand in /opt/molecule-venv/bin/python3 /opt/molecule-venv/bin/python \
              "$(command -v python3 2>/dev/null)" "$(command -v python 2>/dev/null)"; do
    if [ -n "$cand" ] && [ -x "$cand" ] && \
       "$cand" -c "import molecule_runtime" >/dev/null 2>&1; then
      echo "$cand"
      return
    fi
  done
  # Last-resort fallback so the config is still well-formed; the
  # MCP handshake will still fail at runtime, but at install time
  # we surface a warning rather than aborting the whole boot.
  echo "${MOLECULE_MCP_PYTHON:-/opt/molecule-venv/bin/python3}"
}
PYTHON_BIN="$(resolve_python)"
if ! "$PYTHON_BIN" -c "import molecule_runtime" >/dev/null 2>&1; then
  echo "[codex-mcp] WARNING: ${PYTHON_BIN} cannot import molecule_runtime;" \
    "MCP handshake will fail at runtime. Install molecule-ai-workspace-runtime first." >&2
fi

# Resolve the platform-runtime env that the a2a_mcp_server reads on
# startup. Fall back to the same defaults a2a_client.py uses so the
# block is well-formed even when boot order leaves something unset.
WORKSPACE_ID_VAL="${WORKSPACE_ID:-}"
PLATFORM_URL_VAL="${PLATFORM_URL:-http://platform:8080}"
MOLECULE_ORG_ID_VAL="${MOLECULE_ORG_ID:-}"

# CONFIGS_DIR for the MCP child MUST resolve to the SAME directory the
# runtime persists .auth_token into — otherwise the MCP subprocess
# reads a different (empty) path, platform_auth.get_token() returns
# None, and every list_peers / delegate_task call 401s with
# "Authentication to platform failed" while the runtime itself is
# fully authed. This is the codex instance of the Hermes
# list_peers-401 / OpenClaw "MCP wired to the wrong thing" class
# (RFC internal#456 §10).
#
# Root cause of the pre-fix bug: this script hard-defaulted
# CONFIGS_DIR_VAL to "/configs" when CONFIGS_DIR was unset, then wrote
# that literal into [mcp_servers.molecule.env]. configs_dir.resolve()
# treats an explicit CONFIGS_DIR env as an UNCONDITIONAL override (no
# writability check — molecule_runtime/configs_dir.py resolution
# order, see molecule-core#2458), so the MCP child was pinned to
# /configs even when the runtime had (correctly) fallen back to
# $HOME/.molecule-workspace because /configs is root-owned + not
# agent-writable in a fresh container. Result: token file at
# ~/.molecule-workspace/.auth_token, MCP child looking at
# /configs/.auth_token (absent) → 401 → "No peers found".
#
# Fix: ask configs_dir.resolve() itself (the single resolution point
# the runtime uses) what directory it picks, and write THAT. Falls
# back to an explicit operator CONFIGS_DIR if set, then to a literal
# resolve() under the agent HOME so the value is always the one the
# runtime's heartbeat + platform_auth actually use.
_resolve_configs_dir() {
  if [ -n "${CONFIGS_DIR:-}" ]; then
    printf '%s\n' "${CONFIGS_DIR}"
    return
  fi
  # Resolve via the runtime's own single-source-of-truth module, with
  # HOME pinned to the agent home exactly as start.sh runs the helper
  # (HOME=/home/agent). This returns /configs only when it exists AND
  # is agent-writable, otherwise $HOME/.molecule-workspace — i.e. the
  # identical path the runtime will write .auth_token into.
  HOME="${HOME:-/home/agent}" "$PYTHON_BIN" - <<'PY' 2>/dev/null
import molecule_runtime.configs_dir as c
print(c.resolve())
PY
}
CONFIGS_DIR_VAL="$(_resolve_configs_dir)"
# Defensive: if resolve() produced nothing (e.g. runtime import broke),
# fall back to the agent-home path rather than the root-owned /configs
# so a degraded image still avoids the 401-by-misconfig trap.
if [ -z "${CONFIGS_DIR_VAL}" ]; then
  CONFIGS_DIR_VAL="${HOME:-/home/agent}/.molecule-workspace"
fi

# Strip any previous molecule MCP stanza(s) so re-running the script
# every boot doesn't accumulate duplicates. We strip BOTH the parent
# `[mcp_servers.molecule]` header and the `[mcp_servers.molecule.env]`
# subtable header (TOML treats them as independent sections), plus any
# leading auto-generated comment lines that immediately precede them.
# Match from the header through the next [section] header or EOF.
if [ -f "$CONFIG_TOML" ] && grep -qE '^\[mcp_servers\.molecule(\.|])' "$CONFIG_TOML"; then
  awk '
    # Buffer auto-generated comment lines so we can drop them when
    # they precede a stripped header (they belong to the block).
    /^# Auto-generated by codex_mcp_config\.sh/ { buf = buf $0 ORS; next }
    /^# Provides list_peers/                    { buf = buf $0 ORS; next }
    /^# tools to the codex agent/               { buf = buf $0 ORS; next }
    /^\[mcp_servers\.molecule(\.|])/            { skip=1; buf=""; next }
    skip && /^\[/                               { skip=0 }
    !skip                                       { printf "%s", buf; buf=""; print }
    END                                         { printf "%s", buf }
  ' "$CONFIG_TOML" > "${CONFIG_TOML}.tmp" && mv "${CONFIG_TOML}.tmp" "$CONFIG_TOML"
fi

# Append the molecule MCP server block. We use double-equals key form
# inside `env = { ... }` because that's the shape codex's docs/config.md
# documents at rust-v0.57.0. Quoted keys are the safest cross-version
# spelling. `env_vars` is the supplementary passthrough list — anything
# the runtime later adds (MOLECULE_INBOUND_SECRET etc.) gets forwarded
# automatically without a config change.
cat >> "$CONFIG_TOML" <<EOF

# Auto-generated by codex_mcp_config.sh — molecule A2A MCP server.
# Provides list_peers / delegate_task / commit_memory / recall_memory
# tools to the codex agent. See molecule_runtime/a2a_mcp_server.py.
[mcp_servers.molecule]
command = "${PYTHON_BIN}"
args = ["-m", "molecule_runtime.a2a_mcp_server"]
startup_timeout_sec = 30
env_vars = ["MOLECULE_INBOUND_SECRET", "PLATFORM_INBOUND_SECRET", "PYTHONPATH"]

[mcp_servers.molecule.env]
WORKSPACE_ID = "${WORKSPACE_ID_VAL}"
PLATFORM_URL = "${PLATFORM_URL_VAL}"
MOLECULE_ORG_ID = "${MOLECULE_ORG_ID_VAL}"
CONFIGS_DIR = "${CONFIGS_DIR_VAL}"
EOF

# --- Codex sandbox network-access fix (core#2128 / internal#667) -----
#
# Codex 0.130's terminal tool runs every model-generated shell command
# inside an OS-level sandbox (bwrap on Linux). With the default sandbox
# policy — and even with `danger-full-access` — codex on our workspace
# AMI kernel makes bwrap *unshare the network namespace*; bwrap then
# fails to bring up loopback in the new netns:
#
#   bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
#
# so every shell/git/curl/ssh call inside codex agents (the CR2 reviewer
# and Researcher in agents-team) dies and the agent looks dead from the
# outside — the review→merge pipeline stalls.
#
# UPDATE (core#2128, after live-kernel ground-truth 2026-06-04): selecting
# `workspace-write` + `network_access = true` (PR #77) is NOT sufficient.
# It clears the netns/RTM_NEWADDR error, but a workspace-write sandbox ALSO
# has bwrap spawn an unprivileged user namespace and map root into it to
# confine writes — and THAT step fails one later:
#
#   bwrap: setting up uid map: Permission denied
#
# NOT because the kernel blocks userns (it doesn't —
# unprivileged_userns_clone=1, and userns *creation* succeeds), but because
# the agent runs as an unprivileged user (uid 1000, zero effective caps)
# inside an IDENTITY-mapped container (uid_map `0 0 4294967295`, no
# /etc/subuid delegation). Writing a root-mapping uid_map there is refused.
# Reproduced on the live CR2 agent: `unshare --user --map-root-user` →
# `write failed /proc/self/uid_map: Operation not permitted`.
#
# So a write-scoped sandbox cannot initialize on this AMI/container at all.
# The durable fix is to DISABLE codex's inner sandbox
# (`sandbox_mode = "danger-full-access"` + `approval_policy = "never"`) —
# the tenant container is already the isolation boundary (this is exactly
# what the claude-code agents do, and what the live hotpatch uses). The
# earlier shim WIP (fix/bwrap-shim-net_admin-blocker) is the wrong tree;
# PR #77's workspace-write variant is a non-fix. See the PR body.
#
# TOML ordering is load-bearing: `sandbox_mode` is a TOP-LEVEL key, so
# it must appear BEFORE the first `[table]` header (codex_minimax_config
# already writes top-level model/model_provider keys at the top, and we
# write [mcp_servers.molecule] tables below). We therefore PREPEND the
# sandbox_mode line and APPEND the [sandbox_workspace_write] table
# (tables may appear in any order). Both are stripped first so reboots
# stay idempotent — mirrors the molecule-block strip/re-append above.

# Strip any previous auto-generated sandbox stanza so reboots stay
# idempotent: the top-level sandbox_mode + approval_policy lines, either
# marker comment, and the whole [sandbox_workspace_write] table (header
# through next [section] / EOF) — the table strip also cleans up the
# workspace-write leftovers from PR #77 / the older hotpatch.
if [ -f "$CONFIG_TOML" ]; then
  awk '
    /^# Auto-generated by codex_mcp_config\.sh — sandbox/        { next }
    /^# Auto-generated by codex_mcp_config\.sh — disable inner/   { next }
    /^sandbox_mode = /                              { next }
    /^approval_policy = /                           { next }
    /^\[sandbox_workspace_write\]/                  { skip=1; next }
    skip && /^\[/                                    { skip=0 }
    !skip                                            { print }
  ' "$CONFIG_TOML" > "${CONFIG_TOML}.tmp" && mv "${CONFIG_TOML}.tmp" "$CONFIG_TOML"
fi

# Prepend the top-level sandbox keys (before any [table] header).
{
  printf '%s\n' '# Auto-generated by codex_mcp_config.sh — disable inner sandbox (core#2128).'
  printf '%s\n' 'sandbox_mode = "danger-full-access"'
  printf '%s\n' 'approval_policy = "never"'
  cat "$CONFIG_TOML" 2>/dev/null || true
} > "${CONFIG_TOML}.tmp" && mv "${CONFIG_TOML}.tmp" "$CONFIG_TOML"

# No [sandbox_workspace_write] table: with danger-full-access there is no
# inner sandbox, so network_access is moot. (The strip above still removes
# any such table left over from PR #77 / the older hotpatch.)

# Inherit ownership from the codex home dir so the agent user (which
# runs molecule-runtime) can read the config under gosu.
if command -v stat >/dev/null 2>&1; then
  owner=$(stat -c "%u:%g" "$CODEX_HOME" 2>/dev/null || echo "")
  if [ -n "$owner" ]; then
    chown "$owner" "$CONFIG_TOML" 2>/dev/null || true
  fi
fi

echo "[codex-mcp] wrote ${CONFIG_TOML} mcp_servers.molecule python=${PYTHON_BIN} workspace_id=${WORKSPACE_ID_VAL:-<unset>} sandbox=danger-full-access (inner sandbox disabled; tenant container is the isolation boundary)"
