# Molecule AI workspace template — Codex CLI

OpenAI's [Codex CLI](https://github.com/openai/codex) wrapped as a
Molecule workspace runtime, with native MCP-style push parity.

## Why this template exists

Each of the four supported runtimes — claude-code, hermes, openclaw,
codex — needs the same A2A inbox UX: messages from peer agents and
canvas users arrive into the running session, processed in order, with
full conversation continuity.

The naive "shell out to `codex exec --json` per A2A message" approach
loses session continuity (each invocation cold-starts) and pays
process-spawn cost on every turn. This template avoids that by
holding a persistent `codex app-server` child per workspace and
firing `turn/start` RPCs against a single long-lived thread.

See `docs/integrations/codex-app-server-adapter-design.md` in
molecule-core for the full design rationale.

## Layout

| File | Role |
|---|---|
| `adapter.py` | Thin `BaseAdapter` shell — name, display metadata, config schema, preflight, executor factory |
| `executor.py` | `CodexAppServerExecutor` — A2A turn lifecycle, thread bootstrap, notification accumulation, mid-turn serialization |
| `app_server.py` | `AppServerProcess` — async JSON-RPC over NDJSON stdio against the codex app-server child |
| `tests/` | 12 unit tests covering both modules; `mock_app_server.py` is a Python NDJSON stand-in for the real `codex` binary |
| `config.yaml` | Runtime config — model list (OpenAI-only), required env, A2A wiring |
| `Dockerfile` | python:3.11-slim + Node.js 20 + `npm i -g @openai/codex@0.130.0` (exact pin) + molecule_runtime |
| `start.sh` | Verifies codex binary, materializes the ChatGPT/Codex-subscription `auth.json` (Mode C), then exec's molecule-runtime |

## Auth (codex resolves any ONE of these)

Codex needs exactly one credential. Resolution order mirrors OpenClaw's
`openai-codex` provider — an injected subscription `auth.json` is
preferred over the pay-as-you-go API key:

| Credential | How it's supplied | Notes |
|---|---|---|
| `CODEX_AUTH_JSON` | Workspace Config-tab secret bound from Infisical SSOT `/shared/codex-oauth` key `CODEX_AUTH_JSON` (env=prod). `start.sh` writes it to `~/.codex/auth.json` (0600, agent-owned) + sets `cli_auth_credentials_store = "file"` / `forced_login_method = "chatgpt"`. | **Preferred.** ChatGPT/Codex *subscription* OAuth (`auth_mode:"chatgpt"`). SINGLE-RUNNER only — never fan out across concurrent workspaces. `CODEX_CHATGPT_AUTH_JSON` is a backward-compat alias (PR #5); `CODEX_AUTH_JSON` wins if both set. Requires codex CLI ≥ the 0.13x line (this image pins 0.130.0); the legacy 0.57 line cannot consume this format. |
| `OPENAI_API_KEY` | Config-tab env | **Documented fallback.** Pay-as-you-go OpenAI platform key. Retained, not removed. |
| `MINIMAX_API_KEY` | Config-tab env | MiniMax chat-wire route (`codex_minimax_config.sh`). |

## Required env

| Variable | Required | Notes |
|---|---|---|
| one codex credential | Yes | `CODEX_AUTH_JSON` (preferred) **or** `OPENAI_API_KEY` (fallback) **or** `MINIMAX_API_KEY` — see Auth table |
| `MOLECULE_PLATFORM_URL` | Yes | Standard molecule-runtime |
| `MOLECULE_WORKSPACE_ID` | Yes | Standard molecule-runtime |

## Tests

```bash
cd /Users/hongming/Documents/GitHub/molecule-ai-workspace-template-codex
python3 -m pytest tests/ -v
```

12 tests, all pass against a Python NDJSON mock. The `app_server.py`
module is also smoke-tested against the real `codex-cli 0.72.0`
binary; that smoke is one-shot at `/tmp/codex_smoke.py` (not in the
test suite to keep CI fast).

## Status

**Pre-release scaffold (`v0.1.0`).** Modules + tests + container
scaffolding all landed; not yet registered in molecule-core's
`manifest.json` / `runtime_registry.go`, not yet end-to-end verified
against a real Molecule workspace + peer A2A traffic. Both are tracked
under tasks #85 / #86 in the runtime native-MCP work stream.
