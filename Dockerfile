FROM python:3.11-slim

# System deps:
#   curl, ca-certificates — TLS + Node tarball download
#   git           — codex's agent tools use git
#   gosu          — drop privileges in start.sh
#   xz-utils      — Node tarball is .tar.xz
#
# T4 escalation leg (RFC internal#456 §9 / PR#474 — mirrors the
# already-live-verified claude-code template image, commit 12dd604,
# and the in-flight hermes PR#26 / openclaw PR#19):
#   sudo + util-linux(nsenter) + docker.io(CLI) are baked here so the
#   uid-1000 `agent` (see useradd below — UNCHANGED, agent stays
#   uid-1000; start.sh still `exec gosu agent`) has a wired, audited
#   path to host root inside the provisioner's `--privileged
#   --pid=host -v /:/host -v /var/run/docker.sock:/var/run/docker.sock`
#   container. Without sudo, a uid-1000 process in --privileged CANNOT
#   nsenter/chroot /host (--privileged grants caps to root, not
#   uid-1000) and cannot use the root:docker 0660 docker.sock — T4
#   would be provisioner-shape-only (the documented ABSENT-escalation
#   -leg gap; the codex prod pin sha256:877e0687 / git 99e7f13 is the
#   2026-05-06 ECR-mirror rollback that PREDATES all T4 work and ships
#   NO leg). The sudoers drop-in + docker-group add are below, after
#   useradd, so `agent` exists. This is ADDITIVE: it does NOT change
#   the agent uid and does NOT change token ownership. The codex MCP
#   list_peers-401 token-resolution class (RFC internal#456 §10) is
#   fixed atomically in the SAME image revision via codex_mcp_config.sh
#   + start.sh's `chown -R agent:agent /configs`.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git gosu xz-utils \
    sudo util-linux docker.io \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 LTS via NodeSource (codex CLI requires Node ≥20).
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Non-root agent user — UNCHANGED. codex stores sessions under
# ~/.codex/sessions/ so /home/agent should be a persistent volume in
# production deployments to keep thread state across workspace
# restarts. The agent runs as uid-1000; the T4 escalation leg below is
# additive and does NOT promote the agent to root.
RUN useradd -u 1000 -m -s /bin/bash agent

# --- T4 escalation leg (RFC internal#456 §9.3 / PR#474) ---
# Wired path: uid-1000 agent -> host root inside the provisioner's
# --privileged --pid=host -v /:/host -v docker.sock container.
#   1. NOPASSWD sudoers drop-in (mode 0440, visudo-validated at build
#      so a malformed sudoers can never ship a broken-sudo image).
#   2. agent in the `docker` group so the bind-mounted root:docker
#      0660 /var/run/docker.sock is usable without sudo.
# Atomic co-sequencing (RFC §10): this ships in the SAME image
# revision as the uid-1000 + agent-owned-token start.sh contract and
# the codex_mcp_config.sh CONFIGS_DIR resolution fix; the Layer-3
# conformance gate asserts BOTH host-root reach AND agent-owned token
# on the running container. Mirrors claude-code template image
# (12dd604, already live-verified) verbatim.
RUN set -eux; \
    printf 'agent ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/agent-t4; \
    chmod 0440 /etc/sudoers.d/agent-t4; \
    visudo -cf /etc/sudoers.d/agent-t4; \
    groupadd -f docker; \
    usermod -aG docker agent; \
    id agent

WORKDIR /app

# RUNTIME_VERSION arg matches hermes/openclaw conventions — when set
# (cascade-triggered builds), it pins the exact runtime version PyPI
# just published. Including it as ARG changes the cache key for the
# pip install layer below — without this, identical Dockerfile +
# requirements.txt would let docker reuse the cached layer with the
# previous version baked in (the cache trap that bit us 5x on
# 2026-04-27 — see runtime publish pipeline gates memory).
ARG RUNTIME_VERSION=

COPY requirements.txt .
# The codex runtime is registered the SAME way hermes/claude-code do
# it: ENV ADAPTER_MODULE=adapter (set below) — the runtime's adapter
# discovery loads adapter.py and `CodexAdapter.name()` ("codex") is
# authoritative. The previous Dockerfile (inherited from the stale
# single-commit Gitea mirror) ALSO monkeypatched
# `molecule_runtime.preflight.SUPPORTED_RUNTIMES` via an unguarded
# `python3 -c ...add('codex')` + a brittle in-file `sed`. That worked
# against the 2026-05-04 runtime baked into the deployed image
# (sha256:877e0687) but the CURRENT published runtime no longer
# exposes that exact mutable-set literal, so the unguarded RUN exited
# 1 and FAILED THE BUILD (validate-runtime + t4-conformance, CI run
# 1). Root-cause fix: drop the brittle file-rewrite entirely (neither
# hermes nor claude-code patch preflight — adapter discovery is the
# real registration path) and keep only a defensive, idempotent,
# never-fail compatibility shim for any older runtime that still gates
# on a mutable SUPPORTED_RUNTIMES set. `|| true` so a runtime that has
# no such attribute (the modern shape) builds clean.
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ -n "${RUNTIME_VERSION}" ]; then \
      pip install --no-cache-dir --upgrade "molecule-ai-workspace-runtime==${RUNTIME_VERSION}"; \
    fi && \
    python3 -c "import molecule_runtime.preflight as pf; s=getattr(pf,'SUPPORTED_RUNTIMES',None); s.add('codex') if isinstance(s,set) else None; print('preflight SUPPORTED_RUNTIMES shim:', 'patched' if isinstance(s,set) else 'n/a (adapter-module discovery is authoritative)')" || true

COPY adapter.py executor.py app_server.py provider_config.py __init__.py ./
COPY config.yaml ./
COPY start.sh /usr/local/bin/start.sh

# Generic GIT_ASKPASS helper. Reads HTTPS Basic-Auth credentials from
# env vars (GIT_HTTP_USERNAME / GIT_HTTP_PASSWORD, with GITEA_USER /
# GITEA_TOKEN as fallback) and emits them on the git credential-prompt
# protocol, so container-side `git` can authenticate to any private
# HTTPS remote without on-disk .gitconfig / .git-credentials mutation.
# Installed as /usr/local/bin/molecule-askpass — the platform-side
# provisioner sets GIT_ASKPASS to that path. Script body contains no
# hostnames or vendor literals; the deployer decides which remote the
# credentials apply to by virtue of populating those env vars.
COPY scripts/git-askpass.sh /usr/local/bin/molecule-askpass
RUN chmod +x /usr/local/bin/molecule-askpass
# Provider/MCP config helpers — invoked by start.sh on every boot.
#
# render_provider_toml.py is the new YAML-driven entry point: reads
# `providers:` from config.yaml, resolves to the right provider for
# the env, and writes ~/.codex/config.toml accordingly. Replaces the
# legacy hardcoded codex_minimax_config.sh path.
#
# codex_minimax_config.sh is kept as a compat fallback (one release)
# for downstream ops scripts and existing tests that exec it directly;
# start.sh prefers the python helper when available.
#
# codex_mcp_config.sh appends the molecule A2A MCP server block
# (list_peers / delegate_task / commit_memory) and resolves the
# correct CONFIGS_DIR so the MCP child reads the same .auth_token the
# runtime writes (the list_peers-401 fix). start.sh probes both
# /usr/local/bin and /app — install to /usr/local/bin (the primary).
COPY render_provider_toml.py /usr/local/bin/render_provider_toml.py
# provider_config.py is imported by render_provider_toml.py at runtime;
# co-install into /usr/local/bin so the script can find it from there
# (the `_HERE` sys.path insert in render_provider_toml.py picks it up).
# It also lives in /app via the COPY above for adapter.py import.
COPY provider_config.py /usr/local/bin/provider_config.py
COPY codex_minimax_config.sh codex_mcp_config.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/start.sh \
             /usr/local/bin/codex_minimax_config.sh \
             /usr/local/bin/codex_mcp_config.sh \
             /usr/local/bin/render_provider_toml.py

# --- Install the OpenAI Codex CLI globally as root (binary lives in
# /usr/lib/node_modules and symlinks into /usr/bin/codex; available to
# both root and the agent user).
#
# Pinned EXACTLY to 0.130.0 (not a `~`/`^` range). Rationale:
#   * 0.130.0 is the npm `latest` dist-tag — the current stable line
#     (0.131.x is alpha-only at the time of this change; we do not
#     ship a pre-release CLI in a prod runtime image).
#   * The previous `~0.57` pin PREDATES `codex login --device-auth` /
#     ChatGPT-subscription OAuth: it cannot consume the modern
#     `auth.json` shape ({auth_mode:"chatgpt", tokens:{id_token,
#     access_token,refresh_token,account_id}, last_refresh}) and
#     ignores `forced_login_method = "chatgpt"`. The subscription
#     OAuth credential we now materialize (see start.sh Mode C) is
#     only usable on a CLI that supports this format — 0.130.0 does.
#   * config.yaml's default model (`gpt-5.5`) and the May-2026 roster
#     were already live-verified against codex-cli 0.130.0
#     linux/amd64 (thread/start returned "model":"gpt-5.5").
#   * codex's app-server protocol is `experimental` and breaks across
#     minor versions, so we pin the EXACT patch release rather than a
#     range — a bump is a deliberate, reviewed, re-verified change.
RUN npm install -g @openai/codex@0.130.0

USER agent
WORKDIR /home/agent
USER root
WORKDIR /app

ENV ADAPTER_MODULE=adapter \
    PYTHONPATH=/app

# start.sh is intentionally minimal — codex doesn't need a separate
# daemon to boot; the app-server is a stdio child spawned by
# executor.py on the first A2A turn. start.sh also generates the
# MiniMax provider config + molecule MCP block and (as root, before
# the gosu drop) makes /configs agent-owned so the runtime AND the MCP
# child resolve the same agent-owned .auth_token.
ENTRYPOINT ["/usr/local/bin/start.sh"]
