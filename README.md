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
| `Dockerfile` | python:3.11-slim + Node.js 20 + `npm i -g @openai/codex@^0.72` + molecule_runtime |
| `start.sh` | Verifies codex binary + OPENAI_API_KEY, then exec's molecule-runtime |

## Required env

| Variable | Required | Notes |
|---|---|---|
| `OPENAI_API_KEY` | Yes | Codex is OpenAI-only |
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
