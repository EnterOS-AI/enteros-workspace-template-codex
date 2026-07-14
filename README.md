# Molecule AI workspace template — Codex CLI

This repository builds the `codex` workspace image used by Molecule AI. It
wraps [OpenAI Codex CLI](https://github.com/openai/codex)'s persistent
`app-server` behind the common Molecule A2A runtime.

The canonical source is this Gitea repository. Create workspaces through the
canvas runtime picker.

## Runtime shape

- `start.sh` prepares `/configs` and the agent home, configures the provider and
  native MCP descriptor, materializes subscription auth when supplied, starts
  the read-only auth-sync watchdog, and executes `molecule-runtime` as uid 1000.
- `adapter.py` performs credential/provider preflight and creates the Codex
  executor.
- `executor.py` owns the A2A turn lifecycle and a persistent Codex thread.
- `app_server.py` manages JSON-RPC over NDJSON to the long-lived
  `codex app-server` child.
- `config.yaml` defines the template's models/providers. The files under
  `internal/providers/` are a CI-checked registry projection.

This avoids a cold `codex exec` process and lost session on every A2A message.

## Authentication

The current boot and adapter paths accept one of:

| Credential | Use |
|---|---|
| `CODEX_AUTH_JSON` | Preferred single-workspace ChatGPT/Codex subscription auth, materialized to the agent-owned Codex home |
| `CODEX_CHATGPT_AUTH_JSON` | Compatibility alias; `CODEX_AUTH_JSON` wins when both are present |
| `OPENAI_API_KEY` | Direct OpenAI API route |
| `MINIMAX_API_KEY` | MiniMax compatible provider route |

Do not share a subscription auth blob across concurrent workspaces. Credential
values belong in the platform secret surface and must never be committed or
printed.

Provider rendering is owned by `provider_config.py` and `start.sh`. The
platform's resolved provider/base URL values take precedence when present.

## Important files

| Path | Purpose |
|---|---|
| `Dockerfile` | Builds the Codex workspace image and installs the exact CLI version |
| `start.sh` | Container boot, auth, provider, and privilege-drop path |
| `adapter.py` | Adapter contract and preflight |
| `executor.py` | Persistent A2A/Codex turn lifecycle |
| `app_server.py` | Codex app-server JSON-RPC transport |
| `provider_config.py` | Provider selection and TOML model |
| `tests/` | Runtime, auth, provider, MCP, provenance, and documentation contracts |

The current config contains `template_schema_version: 1`; change it only with a
corresponding platform contract change and validation.

## Development and delivery

See [`runbooks/local-dev-setup.md`](runbooks/local-dev-setup.md) for commands
that mirror CI. Pull requests run static, unit, shell, image, and conformance
checks. A push to `main` invokes `publish-image`, which publishes to the Gitea
OCI registry and runs the configured pin verification. Do not substitute a
manual registry script or direct-main-push procedure.

## License

Business Source License 1.1 — © Molecule AI.
