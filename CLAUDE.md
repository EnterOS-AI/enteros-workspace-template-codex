# Coding discipline

1. Think before coding: verify assumptions against current files and tests.
2. Prefer the smallest change that satisfies the task.
3. Keep edits surgical and match the existing style.
4. Define the validation that proves the change before implementing it.

# Repository guide

This is the `codex` workspace image. Its supported path is `start.sh` →
`molecule-runtime` → `CodexAdapter` → `CodexAppServerExecutor` → persistent
`codex app-server` child.

Treat these files as the sources of truth:

| Concern | Source |
|---|---|
| Models/providers | `config.yaml`, `provider_config.py` |
| Container boot and auth materialization | `start.sh` |
| Adapter preflight | `adapter.py` |
| Turn/session behavior | `executor.py`, `app_server.py` |
| Runtime/CLI versions | `.runtime-version`, `requirements.txt`, `Dockerfile` |
| Delivery behavior | `.gitea/workflows/publish-image.yml` |
| Supported local checks | `runbooks/local-dev-setup.md` and `.gitea/workflows/ci.yml` |

Keep credential values out of logs and examples. Do not reintroduce a per-turn
`codex exec` path or a second provider registry. Open a branch and pull request;
never push directly to `main`, tag a release, or manually publish an image as
part of routine work.
