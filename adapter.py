"""Codex CLI adapter — runs OpenAI Codex (`@openai/codex`) inside the workspace.

This template wraps OpenAI's Codex CLI as a Molecule workspace runtime.
The actual A2A bridge lives in ``executor.py`` — this file is just the
``BaseAdapter`` shell: name, display metadata, config schema, executor
factory, and an ``OPENAI_API_KEY`` reachability check at setup.

Architecture in one paragraph: each workspace session holds one
long-lived ``codex app-server`` child (spawned by ``executor.py`` on
first turn) plus one Codex thread. A2A messages become ``turn/start``
RPCs against that thread, giving us session continuity + queued
mid-turn handling. See
``docs/integrations/codex-app-server-adapter-design.md`` in
molecule-core for the full design.

We deliberately do NOT run a separate daemon here (unlike hermes,
where a long-running gateway listens on :8642 from container boot).
``codex app-server`` is a stdio child of the executor, not a network
service — fewer moving parts, no port to configure, no health endpoint
to wait on at start time.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from molecule_runtime.adapters.base import BaseAdapter, AdapterConfig

logger = logging.getLogger(__name__)


class CodexAdapter(BaseAdapter):
    """Adapter that proxies A2A turns to a persistent codex app-server."""

    @staticmethod
    def name() -> str:
        return "codex"

    @staticmethod
    def display_name() -> str:
        return "OpenAI Codex CLI"

    @staticmethod
    def description() -> str:
        return (
            "Runs the OpenAI Codex CLI (@openai/codex) with native session "
            "continuity. Each A2A message becomes a turn against a "
            "long-lived codex thread — same UX shape as hermes/openclaw, "
            "MCP-native push parity with claude-code."
        )

    @staticmethod
    def get_config_schema() -> dict:
        return {
            "model": {
                "type": "string",
                "description": (
                    "Codex model. Pass through to `thread/start`. May-2026 "
                    "roster: 'gpt-5.5' (default), 'gpt-5.4', 'gpt-5.4-mini', "
                    "'gpt-5.3-codex', 'gpt-5.3-codex-spark', 'gpt-5.2'. "
                    "Empty = codex default (gpt-5.5)."
                ),
            },
            "provider": {
                "type": "string",
                "description": (
                    "Optional codex provider id from the `providers:` "
                    "registry in config.yaml (e.g. 'openai-subscription', "
                    "'openai-api', 'minimax-token-plan'). Empty = "
                    "auto-resolve from model + env credentials."
                ),
            },
        }

    async def setup(self, config: AdapterConfig) -> None:
        """Verify the codex binary is on PATH and a credential is set, then
        render ``~/.codex/config.toml`` from the providers registry.

        We do NOT spawn the app-server here — that happens lazily on
        the first turn inside the executor. Failing fast at setup
        time with a clear message beats a confusing ``FileNotFoundError``
        from the executor's first ``asyncio.create_subprocess_exec``.

        Provider resolution (see ``provider_config.resolve_provider``):
          1. Explicit ``provider`` field in ``runtime_config`` /
             ``MODEL_PROVIDER`` env wins.
          2. Else, if any ``chatgpt_subscription`` provider's auth_env
             is set (``CODEX_AUTH_JSON`` / ``CODEX_CHATGPT_AUTH_JSON``),
             pick it — preserves the verified prod behavior where the
             subscription beats a co-set vendor key.
          3. Else, model-prefix / alias match against the registry.
          4. Else, first credential-satisfied entry, with the registry's
             first entry as the final fallback.

        The resolved provider is then rendered to ``~/.codex/config.toml``:
        built-in modes (subscription, openai_api) emit NO override (the
        CLI's native OpenAI/Responses provider handles them); compat
        providers emit ``[model_providers.<slug>]`` + ``model_provider``.
        """
        if not shutil.which("codex"):
            raise RuntimeError(
                "codex binary not on PATH. The Dockerfile installs "
                "@openai/codex globally via npm — if you're running "
                "outside the container, install it with: "
                "`npm install -g @openai/codex`"
            )
        # Auth: codex resolves credentials in three ways and any one
        # is sufficient. Mirror that here so setup() does not
        # false-fail a validly-authed workspace:
        #   A. OPENAI_API_KEY  — direct OpenAI path (codex default).
        #   B. MINIMAX_API_KEY — MiniMax chat-wire route
        #      (codex_minimax_config.sh writes config.toml).
        #   C. $CODEX_HOME/auth.json — an injected ChatGPT/Codex
        #      -subscription credential (auth_mode:"chatgpt"),
        #      materialized by start.sh from the CODEX_AUTH_JSON env
        #      var (Infisical SSOT /shared/codex-oauth, key
        #      CODEX_AUTH_JSON, env=prod; CODEX_CHATGPT_AUTH_JSON is a
        #      backward-compat alias) for a SINGLE runner. This mirrors
        #      OpenClaw's openai-codex auth.order: prefer an injected
        #      subscription auth.json over the pay-as-you-go API key.
        #      Codex prefers auth.json over env keys. The
        #      OPENAI_API_KEY path (A) is retained as the documented
        #      fallback and is intentionally NOT removed.
        # CODEX_HOME defaults to ~/.codex; honor an explicit override
        # so a non-default home is still detected.
        codex_home = os.environ.get("CODEX_HOME") or os.path.join(
            os.path.expanduser("~"), ".codex"
        )
        auth_json = Path(codex_home) / "auth.json"
        has_auth_json = auth_json.is_file() and auth_json.stat().st_size > 0
        if not (
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("MINIMAX_API_KEY")
            or has_auth_json
        ):
            raise RuntimeError(
                "No codex credential found. Codex needs exactly one "
                "of: OPENAI_API_KEY (direct OpenAI), MINIMAX_API_KEY "
                "(MiniMax token-plan codex route), or an injected "
                "ChatGPT/Codex-subscription auth.json at "
                f"{auth_json} (set CODEX_AUTH_JSON for a single-runner "
                "workspace). Configure via the canvas Config tab."
            )

        # --- Provider resolution + config.toml rendering ---
        # Pull the picked model + (optional) explicit provider from
        # runtime_config (the canvas Config tab writes here on Save).
        rc = getattr(config, "runtime_config", None)
        if isinstance(rc, dict):
            yaml_model = rc.get("model") or ""
            yaml_provider = rc.get("provider") or ""
        else:
            yaml_model = getattr(rc, "model", None) or getattr(config, "model", "") or ""
            yaml_provider = getattr(rc, "provider", None) or ""

        try:
            from provider_config import (
                assert_model_is_not_provider_name,
                load_providers, resolve_provider, write_config_toml,
            )
        except ImportError as exc:
            # Defensive: fall back to the legacy shell-script path
            # below if the module can't be imported (e.g. a partial
            # install). The credential preflight above has already
            # gated; codex will boot off OPENAI_API_KEY or auth.json
            # using the CLI defaults.
            logger.warning(
                "codex: provider_config import failed (%s); "
                "skipping registry-driven config.toml render",
                exc,
            )
            return

        providers = load_providers(
            workspace_config_path=getattr(config, "config_path", "") or "",
        )

        # MODEL_PROVIDER is historically overloaded in the platform
        # stack: old provisioners used it for a model id, while newer
        # config paths use it for a provider name. Treat it as explicit
        # only when it names a provider the registry actually accepts.
        # A leaked value like "gpt-5.5" must not override the
        # subscription auto-detection path.
        env_provider = (os.environ.get("MODEL_PROVIDER") or "").strip()
        provider_names = {p["name"].lower() for p in providers}
        if env_provider and env_provider.lower() in provider_names:
            explicit_provider = env_provider
        elif env_provider:
            explicit_provider = yaml_provider or None
            logger.warning(
                "codex adapter: ignoring legacy MODEL_PROVIDER=%r because "
                "it is not a provider registry name",
                env_provider,
            )
        else:
            explicit_provider = yaml_provider or None

        # Platform-managed LLM: force the `platform` provider (proxy Responses
        # surface) regardless of MODEL_PROVIDER/yaml. In this mode the tenant
        # has no BYOK key (the workspace-server strips them); the proxy owns
        # the keys + usage billing. The base_url is overridden below with the
        # injected MOLECULE_LLM_BASE_URL (per-env), and codex POSTs
        # {base_url}/responses (wire_api=responses).
        platform_managed = os.environ.get("MOLECULE_LLM_BILLING_MODE") == "platform_managed"
        if platform_managed:
            if "platform" not in provider_names:
                raise RuntimeError(
                    "codex adapter: MOLECULE_LLM_BILLING_MODE=platform_managed but no "
                    "`platform` provider in the registry — add it to config.yaml `providers:`"
                )
            explicit_provider = "platform"

        # Defense-in-depth for the CP workspace-config writer bug
        # (2026-05-18 Reviewer + Researcher wedge): if the upstream
        # writer stamped a PROVIDER name into the YAML `model:` field
        # (e.g. model: 'openai-subscription'), refuse to boot rather
        # than letting codex thread/start accept the garbage and wedge.
        # Either side alone closes the bug — see
        # `assert_model_is_not_provider_name` doc + the structural fix
        # in molecule-controlplane's userdata_containerized.go /
        # ec2.go writer.
        assert_model_is_not_provider_name(yaml_model, providers)

        try:
            picked = resolve_provider(
                yaml_model, providers,
                explicit_provider=explicit_provider,
            )
        except ValueError:
            # Re-raise with the actionable message intact — silent
            # fallback to providers[0] when the operator picked an
            # unknown name would route them through the wrong
            # base_url + env key (the analog #180 in claude-code).
            raise

        # Platform-managed: prefer the injected per-env proxy base over the
        # registry's static (prod) base_url, so staging routes to staging.
        if platform_managed:
            base = (os.environ.get("MOLECULE_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").strip()
            if base:
                picked = {**picked, "base_url": base.rstrip("/")}

        # Render + write config.toml. For built-in OpenAI auth modes
        # (subscription, openai_api) this writes NOTHING and clears
        # any stale auto-generated override — exactly the verified
        # device-logged codex-0.130 shape that the prod-Reviewer /
        # prod-Researcher path requires.
        codex_home = os.environ.get("CODEX_HOME") or os.path.join(
            os.path.expanduser("~"), ".codex"
        )
        try:
            written = write_config_toml(
                picked, model=yaml_model or None, codex_home=codex_home,
            )
        except ValueError as exc:
            # Misconfigured registry entry (missing base_url / vendor
            # env). Fail closed so the operator sees the YAML defect.
            raise RuntimeError(
                f"codex provider registry: {exc}"
            ) from exc

        logger.info(
            "codex adapter: provider=%s auth_mode=%s wrote=%s",
            picked["name"], picked["auth_mode"],
            str(written) if written else "<no override>",
        )

        # --- Plugin pipeline: render declared plugins into ~/.codex/config.toml ---
        # Drive the base per-runtime plugin adaptor pipeline. For an MCP-server
        # plugin (e.g. the privileged ``molecule-platform-mcp`` the concierge
        # declares) this resolves to MCPServerAdaptor, which calls
        # ``register_mcp_server_hook`` — our override below — to render the
        # ``[mcp_servers.<name>]`` table into the codex config.toml the running
        # CLI reads. Without this call the declared management MCP is NEVER
        # written for codex, so a codex concierge boots without create_workspace
        # (the #3159 class of bug — wired-to-a-file-the-runtime-never-reads, here
        # specifically: not wired at all). Mirrors the claude-code adapter's
        # ``install_plugins_via_registry`` call from its own setup().
        from molecule_runtime.plugins import load_plugins
        workspace_plugins_dir = os.path.join(config.config_path, "plugins")
        plugins = load_plugins(
            workspace_plugins_dir=workspace_plugins_dir,
            shared_plugins_dir=os.environ.get("PLUGINS_DIR", "/plugins"),
        )
        await self.install_plugins_via_registry(config, plugins)

        # --- SSOT: publish the single base-built system prompt onto config ---
        # The codex executor consumes ``config.system_prompt`` as
        # ``developerInstructions`` (executor.py). That field is BASE-OWNED and
        # is None until something fills it. Build it HERE via the one canonical
        # builder (``build_system_prompt``), which honors ``config.prompt_files``
        # (with the legacy ``system-prompt.md`` fallback baked in) — so the codex
        # concierge gets the SAME prompt-file resolution every other runtime
        # gets, instead of an empty ``developerInstructions``. This closes the
        # per-runtime prompt drift (the executor must never re-read
        # /configs/system-prompt.md itself and ignore prompt_files). Plugin
        # rules/prompts already loaded above are folded in so the assembled
        # prompt matches the base ``_common_setup`` shape.
        from molecule_runtime.prompt import build_system_prompt
        config.system_prompt = build_system_prompt(
            config.config_path,
            config.workspace_id,
            [],  # skills: codex does not load LangChain skills into the prompt
            [],  # peers: fetched live per-turn by the platform tools, not baked
            prompt_files=config.prompt_files,
            plugin_rules=getattr(plugins, "rules", None),
            plugin_prompts=list(getattr(plugins, "prompt_fragments", []) or []),
        )

    def register_mcp_server_hook(self, config, name, spec):
        """Codex MCP-wiring PORT override: inject literal molecule-* env values.

        Codex's MCP-child env whitelist (codex-rs/rmcp-client/src/utils.rs) only
        forwards a small set (HOME / PATH / LANG / …) and DROPS the molecule-
        specific runtime env — so an MCP server spawned by codex never inherits
        ``MOLECULE_CP_URL`` / ``MOLECULE_ADMIN_TOKEN`` / ``PLATFORM_URL`` / etc.
        from the parent process the way Claude Code's MCP child does. The
        management MCP (``@molecule-ai/mcp-server``) reads MOLECULE_CP_URL +
        MOLECULE_ADMIN_TOKEN to reach the controlplane; without them
        create_workspace 401s/no-ops even though the server is declared.

        Fix: resolve those values at install time and merge them as LITERALS
        into the spec's ``env`` block before the base renderer writes the codex
        ``[mcp_servers.<name>.env]`` sub-table — the exact pattern the hardcoded
        ``[mcp_servers.molecule]`` a2a block uses for WORKSPACE_ID / PLATFORM_URL
        (codex_mcp_config.sh). Values already present in the plugin descriptor's
        env (e.g. MOLECULE_MCP_MODE) are preserved and win, so this only fills
        the whitelist-dropped gaps. The base hook then dispatches on self.name()
        == "codex" to render_codex_config (~/.codex/config.toml).
        """
        spec = dict(spec)
        descriptor_env = dict(spec.get("env") or {})

        # The molecule-* runtime env codex would otherwise drop. Only keys whose
        # value is actually present in this process are injected (an empty string
        # written into the TOML would shadow nothing useful and could confuse the
        # MCP server's "is this configured?" checks). Descriptor-declared keys
        # are NOT overwritten.
        for key in (
            "MOLECULE_CP_URL",
            "MOLECULE_ADMIN_TOKEN",
            "PLATFORM_URL",
            "WORKSPACE_ID",
            "MOLECULE_ORG_ID",
        ):
            if key in descriptor_env:
                continue
            val = os.environ.get(key)
            if val:
                descriptor_env[key] = val

        if descriptor_env:
            spec["env"] = descriptor_env
        return super().register_mcp_server_hook(config, name, spec)

    async def create_executor(self, config: AdapterConfig):
        from executor import CodexAppServerExecutor
        return CodexAppServerExecutor(config)


Adapter = CodexAdapter
