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
                    "Codex model. Pass through to `thread/start`. Common: "
                    "'gpt-5', 'gpt-5-mini', 'o4-mini'. Empty = codex default."
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
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY is required for the codex runtime. "
                "Set it in the workspace's environment via the canvas "
                "Config tab."
            )

    async def create_executor(self, config: AdapterConfig):
        from executor import CodexAppServerExecutor
        return CodexAppServerExecutor(config)


Adapter = CodexAdapter
