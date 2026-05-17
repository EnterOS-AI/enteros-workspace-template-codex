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

import os
import shutil
from pathlib import Path

from molecule_runtime.adapters.base import BaseAdapter, AdapterConfig


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
        }

    async def setup(self, config: AdapterConfig) -> None:
        """Verify the codex binary is on PATH and OPENAI_API_KEY is set.

        We do NOT spawn the app-server here — that happens lazily on
        the first turn inside the executor. Failing fast at setup
        time with a clear message beats a confusing ``FileNotFoundError``
        from the executor's first ``asyncio.create_subprocess_exec``.
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
        #   C. $CODEX_HOME/auth.json — an injected
        #      ChatGPT-subscription credential (auth_mode:chatgpt),
        #      written by start.sh from CODEX_CHATGPT_AUTH_JSON for a
        #      SINGLE runner. Codex prefers auth.json over env keys.
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
                "(MiniMax chat-wire route), or an injected "
                "ChatGPT-subscription auth.json at "
                f"{auth_json} (set CODEX_CHATGPT_AUTH_JSON for a "
                "single-runner workspace). Configure via the canvas "
                "Config tab."
            )

    async def create_executor(self, config: AdapterConfig):
        from executor import CodexAppServerExecutor
        return CodexAppServerExecutor(config)


Adapter = CodexAdapter
