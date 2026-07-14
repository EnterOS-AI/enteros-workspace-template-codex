# Local development — Codex workspace template

These commands follow the current repository CI. Local unit tests use controlled
app-server/runtime doubles and do not require a live workspace or real Codex
credential.

## Prerequisites

- Python 3.11+
- Git
- Access to `git.moleculesai.app` and its package registry
- `shellcheck` for the shell gate
- Docker only when reproducing the image/conformance jobs

## Clone and create an isolated environment

```bash
git clone https://git.moleculesai.app/molecule-ai/molecule-ai-workspace-template-codex.git
cd molecule-ai-workspace-template-codex
git switch -c fix/describe-the-change

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install pytest pytest-asyncio pyyaml packaging jsonschema
```

For the canonical template validator and real private-runtime import:

```bash
rm -rf .molecule-ci-canonical
git clone --depth 1 https://git.moleculesai.app/molecule-ai/molecule-ci.git .molecule-ci-canonical
python3 .molecule-ci-canonical/scripts/install_workspace_dependencies.py --allow-missing
```

## Run the checks

```bash
PROVIDERS_MANIFEST_FILE=internal/providers/providers.yaml \
  python3 .molecule-ci-canonical/scripts/validate-workspace-template.py --static-only
shellcheck -S error start.sh codex_minimax_config.sh codex_mcp_config.sh codex_auth_sync.sh
python3 -m pytest tests/ -v
```

The test suite covers the app-server protocol through
`tests/mock_app_server.py`; it does not require a temporary script outside the
repository or a hard-coded local checkout path.

## Build the image

With Docker and package access available:

```bash
docker build -t workspace-template-codex:dev .
```

The supported container boot is `start.sh`, not direct execution of
`adapter.py`. A platform-shaped run also requires `/configs`, `/workspace`,
runtime identity, and a real provider credential. CI owns the image smoke and
privilege-conformance probes.

## Before opening a pull request

```bash
git diff --check
python3 -m pytest tests/test_current_documentation.py -q
```

Never commit `.env` files, API keys, subscription auth JSON, refreshed auth
state, or generated Codex configuration.
