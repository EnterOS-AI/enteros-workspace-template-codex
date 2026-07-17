FROM python:3.11-slim

# System deps:
#   curl, ca-certificates — TLS + Node tarball download
#   git           — codex's agent tools use git
#   gosu          — drop privileges in start.sh
#   xz-utils      — Node tarball is .tar.xz
#
# T4 escalation leg (RFC internal#456 §9 / PR#474 — mirrors the
# live-verified sibling workspace templates):
#   sudo + util-linux(nsenter) + docker.io(CLI) are baked here so the
#   uid-1000 `agent` (see useradd below — UNCHANGED, agent stays
#   uid-1000; start.sh still `exec gosu agent`) has a wired, audited
#   path to host root inside the provisioner's `--privileged
#   --pid=host -v /:/host -v /var/run/docker.sock:/var/run/docker.sock`
#   container. Without sudo, a uid-1000 process in --privileged CANNOT
#   nsenter/chroot /host (--privileged grants caps to root, not
#   uid-1000) and cannot use the root:docker 0660 docker.sock — T4
#   would be provisioner-shape-only. The sudoers drop-in + docker-group
#   add are below, after
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
    groupadd -g 988 -f docker-host || true; \
    usermod -aG docker agent; \
    usermod -aG docker-host agent || true; \
    id agent

WORKDIR /app

# RUNTIME_VERSION arg matches the other official adapters — when set
# (cascade-triggered builds), it pins the exact runtime version the private
# registry just published. Including it as ARG changes the cache key for the
# runtime wheel layer below — without this, identical Dockerfile +
# requirements.txt would let docker reuse the cached layer with the
# previous version baked in (the cache trap that bit us 5x on
# 2026-04-27 — see runtime publish pipeline gates memory).
ARG RUNTIME_VERSION=
ARG MOLECULE_RUNTIME_INDEX=https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/

COPY requirements.txt .
COPY scripts/prepare_runtime_requirements.py /tmp/prepare_runtime_requirements.py
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
# Acquire the private runtime as a local wheel before resolving public deps.
# pip does not prioritize index-url over extra-index-url, so a mixed-index
# runtime solve can select a public namesake with a higher version. The isolated
# download consults only Gitea and fetches no dependencies; the subsequent solve
# receives that local wheel explicitly and resolves all public dependencies from
# the default index.
RUN set -eux; \
    runtime_project="molecules-workspace-runtime"; \
    runtime_requirement="$(python3 /tmp/prepare_runtime_requirements.py \
      requirements.txt /tmp/template-requirements.txt \
      --runtime-version "${RUNTIME_VERSION}")"; \
    case "${runtime_requirement}" in "${runtime_project}"*) ;; *) exit 1 ;; esac; \
    rm -rf /tmp/molecule-runtime; \
    mkdir -p /tmp/molecule-runtime; \
    pip download --isolated --no-cache-dir --only-binary=:all: --no-deps \
      --index-url "${MOLECULE_RUNTIME_INDEX}" \
      --dest /tmp/molecule-runtime "${runtime_requirement}"; \
    test "$(find /tmp/molecule-runtime -maxdepth 1 -type f -name '*.whl' | wc -l)" -eq 1; \
    pip install --isolated --no-cache-dir /tmp/molecule-runtime/*.whl \
      -r /tmp/template-requirements.txt; \
    rm -rf /tmp/molecule-runtime /tmp/template-requirements.txt \
      /tmp/prepare_runtime_requirements.py; \
    python3 -c "import molecule_runtime.preflight as pf; s=getattr(pf,'SUPPORTED_RUNTIMES',None); s.add('codex') if isinstance(s,set) else None; print('preflight SUPPORTED_RUNTIMES shim:', 'patched' if isinstance(s,set) else 'n/a (adapter-module discovery is authoritative)')" || true

# --- Pre-bake the management-MCP server (base-runtime helper; task #54) ---
# The kind=platform concierge launches `npx --prefer-offline @molecule-ai/mcp-server@<PIN>`
# in a HARD-deadline enumeration spawn at boot; without a warm cache it cold-pulls
# -> ETARGET / CF-WAF throttle -> #1027 fail-close (launch-side of RCA #2970). The bake
# LOGIC + the pinned version now live ONCE in the base runtime (molecule_runtime, pinned
# to the SDK contract management_mcp_server block) — this template DELEGATES to the shared
# helper instead of carrying its own bake + ARG (ADR-004: SDK contract -> base-runtime
# default -> per-adapter override-if-needed; no per-template fork). Replaces the former
# per-template bake that had drifted to a STALE 1.8.1 pin (the plugin fragment pins 1.8.2)
# — the SSOT delegation always bakes the contract pin. codex ships node globally on PATH,
# so no MOLECULE_PREBAKE_NODE_BIN override. The helper's build-time OFFLINE self-check
# fails the image if the bake is broken.
USER agent
RUN bash "$(python3 -c 'import molecule_runtime, os; print(os.path.dirname(molecule_runtime.__file__))')/scripts/prebake-mgmt-mcp.sh"
USER root


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
COPY scripts/molecule-askpass /usr/local/bin/molecule-askpass
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
# codex_auth_refresh.sh — OAuth refresh watchdog (RFC internal#569).
# start.sh launches it as `gosu agent` after auth.json is materialized;
# it polls every 6h and rewrites auth.json atomically when the access
# token is within 4h of expiry OR last_refresh is older than 7d. Inert
# when no auth.json is present (the API-key / MiniMax paths skip it).
COPY codex_auth_refresh.sh /usr/local/bin/codex_auth_refresh.sh
# codex_auth_sync.sh — GET-ONLY auth.json re-sync watchdog (codex shared-OAuth
# durable fix, 2026-05-31). start.sh runs it `--once` synchronously BEFORE the
# codex app-server launches so a stale persisted auth.json is overwritten with
# the platform's CURRENT token (the stale token is what triggers the 401→burn),
# then loops hourly. It NEVER POSTs to any OAuth endpoint — rotation is the
# platform central refresher's job; agents only re-sync.
COPY codex_auth_sync.sh /usr/local/bin/codex_auth_sync.sh
RUN chmod +x /usr/local/bin/start.sh \
             /usr/local/bin/codex_minimax_config.sh \
             /usr/local/bin/codex_mcp_config.sh \
             /usr/local/bin/render_provider_toml.py \
             /usr/local/bin/codex_auth_refresh.sh \
             /usr/local/bin/codex_auth_sync.sh

# Build-time smoke check for the OAuth refresh watchdog (PR#24
# regression-pin). Pre-PR#24 the script hardcoded
# /opt/molecule-venv/bin/python3, a path that does NOT exist in this
# image (we build FROM python:3.11-slim → python3 at
# /usr/local/bin/python3). Every helper invocation exited 127, OAuth
# refresh never fired, id_token expired silently, Researcher wedged
# upstream of stdout (ae2c3012 diagnosis). This RUN executes the
# watchdog's `--once` path against an absent CODEX_HOME — which exercises
# the python3 resolver AND the absent-auth.json skip branch. Expected
# rc=1 (skip:no_auth_json); rc=127 means the python3 path regressed and
# the image must fail to build, NEVER ship.
RUN set -eux; \
    bash -n /usr/local/bin/codex_auth_refresh.sh; \
    rc=0; \
    CODEX_HOME=/tmp/.codex-smoke-no-auth /usr/local/bin/codex_auth_refresh.sh --once || rc=$?; \
    rm -rf /tmp/.codex-smoke-no-auth; \
    if [ "$rc" -eq 127 ]; then \
      echo "FATAL: codex_auth_refresh.sh exited 127 at image-build smoke — python3 helper not located. PR#19 OAuth auto-refresh would ship broken (PR#24 regression-pin)." >&2; \
      exit 1; \
    fi; \
    if [ "$rc" -ne 1 ]; then \
      echo "FATAL: codex_auth_refresh.sh smoke produced rc=$rc (expected rc=1 skip:no_auth_json). Image-build watchdog smoke failed." >&2; \
      exit 1; \
    fi; \
    echo "[image-build smoke] codex_auth_refresh.sh OAuth watchdog OK (rc=1 skip:no_auth_json — python3 helper resolves)."

# Build-time smoke for the GET-only re-sync watchdog (mirrors the refresh
# smoke). `bash -n` syntax-checks it, then `--once` runs against an ABSENT
# CODEX_HOME — which exercises the python3 resolver and the inert no-CODEX_HOME
# skip branch (rc=1). rc=127 means python3 didn't resolve → the re-sync would
# ship broken and the stale-token burn would recur, so FAIL the build.
RUN set -eux; \
    bash -n /usr/local/bin/codex_auth_sync.sh; \
    rc=0; \
    CODEX_HOME=/tmp/.codex-sync-smoke-absent /usr/local/bin/codex_auth_sync.sh --once || rc=$?; \
    rm -rf /tmp/.codex-sync-smoke-absent; \
    if [ "$rc" -eq 127 ]; then \
      echo "FATAL: codex_auth_sync.sh exited 127 at image-build smoke — python3 helper not located. The codex auth re-sync would ship broken and the shared-token burn would recur." >&2; \
      exit 1; \
    fi; \
    if [ "$rc" -ne 1 ]; then \
      echo "FATAL: codex_auth_sync.sh smoke produced rc=$rc (expected rc=1 skip: absent CODEX_HOME). Image-build re-sync smoke failed." >&2; \
      exit 1; \
    fi; \
    echo "[image-build smoke] codex_auth_sync.sh re-sync watchdog OK (rc=1 skip:absent CODEX_HOME — python3 helper resolves)."

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

# GIT_ASKPASS wiring (cp#444 prerequisite). The Dockerfile comment on the
# molecule-askpass COPY above (and the script's own header) states "the
# platform-side provisioner sets GIT_ASKPASS to that path" — but NO
# molecule-controlplane provisioner ever set GIT_ASKPASS (grep of
# internal/provisioner/*.go returns nothing). So at runtime GIT_ASKPASS was
# unset, git never invoked the helper, and the per-agent GITEA_TOKEN that
# cp#444 (secretsDPreferredKeys → secrets.d/load.sh) delivers was never used
# for git-over-HTTPS — codex agents (CR2/Researcher) could not auth to Gitea.
#
# GIT_ASKPASS is a uniform, non-secret, per-IMAGE constant (the helper path is
# the same in every codex container; the per-agent variation is the TOKEN
# VALUE, delivered separately via secrets.d). That makes it a Dockerfile ENV,
# not a per-workspace provisioner concern — and baking it into the image layer
# means it survives container recreate/restart, which is the whole point of
# cp#444 (the provision-time env-file is rewritten on every recreate).
#
# Build-safe: molecule-askpass (read at COPY above, /usr/local/bin) emits an
# empty string and exits 0 when GIT_HTTP_PASSWORD/GITEA_TOKEN are unset — it
# never errors. There are no authenticated build-time `git clone` steps in
# this image (git is used only by runtime agent tools; NodeSource/pip/npm use
# curl + the PyPI index URL, not authenticated git), so setting GIT_ASKPASS
# cannot affect the build. It only takes effect when a runtime git invocation
# hits an HTTPS auth challenge.
ENV ADAPTER_MODULE=adapter \
    PYTHONPATH=/app \
    GIT_ASKPASS=/usr/local/bin/molecule-askpass

# start.sh is intentionally minimal — codex doesn't need a separate
# daemon to boot; the app-server is a stdio child spawned by
# executor.py on the first A2A turn. start.sh also generates the
# MiniMax provider config + molecule MCP block and (as root, before
# the gosu drop) makes /configs agent-owned so the runtime AND the MCP
# child resolve the same agent-owned .auth_token.
ENTRYPOINT ["/usr/local/bin/start.sh"]
