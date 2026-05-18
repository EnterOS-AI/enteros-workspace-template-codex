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

        # MODEL_PROVIDER env from the persona-env layer (if any) wins
        # over YAML when set — mirrors the claude-code template's
        # _resolve_model_and_provider_from_env shape.
        env_provider = (os.environ.get("MODEL_PROVIDER") or "").strip()
        explicit_provider = env_provider or yaml_provider or None

        try:
            from provider_config import (
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

    async def create_executor(self, config: AdapterConfig):
        from executor import CodexAppServerExecutor
        return CodexAppServerExecutor(config)


Adapter = CodexAdapter
