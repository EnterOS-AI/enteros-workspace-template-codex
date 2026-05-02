"""A2A → codex app-server bridge.

Holds one persistent `codex app-server` child + one thread per
workspace session, dispatches each A2A message as a `turn/start` RPC
against the existing thread.

Design rationale lives in
``docs/integrations/codex-app-server-adapter-design.md`` (in
molecule-core). The short version:

- Persistent child gives us session continuity (the agent's
  conversation history, tool state, and config persist across A2A
  turns) without serializing through disk.
- Per-thread serialization (``_turn_lock``) gives us safe, ordered
  handling of mid-turn arrivals — A2A peers see their messages
  processed in arrival order, matching OpenClaw's per-chat
  sequentializer behavior.
- Notification-driven response assembly: the executor accumulates
  ``agent_message_delta`` chunks and emits the final assembled text
  on ``turn/completed``. Streaming forward is a future upgrade once
  the molecule-runtime contract supports incremental events.

The riskiest module of this stack is ``app_server.AppServerProcess``
(the raw JSON-RPC client) — that has its own unit tests. This file
focuses on the protocol-level lifecycle: thread bootstrap, turn
dispatch, notification accumulation, error surface.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from a2a.helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue

from molecule_runtime.adapters.base import AdapterConfig
from molecule_runtime.executor_helpers import extract_message_text

from app_server import AppServerError, AppServerProcess

logger = logging.getLogger(__name__)


# Per-turn timeout. Codex turns can run minutes during heavy tool use
# (test runs, edits, web fetches). Tighter than infinite to bound
# debug-time hangs.
_TURN_TIMEOUT = 600.0


@dataclass
class _TurnState:
    """Mutable state accumulated during one turn lifecycle.

    Owned by the running ``_run_turn`` invocation; the notification
    subscriber appends to it under ``_turn_lock``.
    """
    deltas: list[str] = field(default_factory=list)
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    error: Exception | None = None
    turn_id: str | None = None


class CodexAppServerExecutor(AgentExecutor):
    """A2A executor that proxies turns to a long-lived codex app-server."""

    def __init__(self, config: AdapterConfig):
        self._config = config
        self._app_server: AppServerProcess | None = None
        self._thread_id: str | None = None
        # Serialize turns per thread. mid-turn A2A arrivals queue and
        # run after the current turn completes — same shape OpenClaw's
        # per-chat sequentializer uses.
        self._turn_lock = asyncio.Lock()
        # Tracked so cancel() can fire turn/interrupt against the
        # currently-running turn (best-effort).
        self._current_turn_id: str | None = None

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------
    async def _ensure_thread(self) -> str:
        """Lazy-init the app-server child + thread on first turn."""
        if self._app_server is None:
            env = {
                # Codex picks up OPENAI_API_KEY from the environment.
                # We pass through everything; container start.sh is
                # responsible for ensuring the key is present.
                **os.environ,
            }
            self._app_server = await AppServerProcess.start(env=env)
            await self._app_server.initialize(client_info={
                "name": "molecule-runtime-codex",
                "version": "0.1.0",
            })
            logger.info("codex app-server child initialized")

        if self._thread_id is None:
            params: dict[str, Any] = {}
            if self._config.model:
                params["model"] = self._config.model
            if self._config.system_prompt:
                params["developerInstructions"] = self._config.system_prompt
            # Workspace agents can't prompt a human, so approval policy
            # must be `never`. Sandbox `workspace-write` lets the agent
            # edit the workspace tree but not arbitrary disk.
            params["approvalPolicy"] = "never"
            params["sandboxPolicy"] = {"mode": "workspace-write"}

            resp = await self._app_server.request("thread/start", params)
            # Field name varies between the v2 JSON schema (threadId) and
            # the running binary 0.72.x (id). Accept either — verified
            # 2026-05-02 against codex-cli 0.72.0 which returns `id`.
            thread = resp.get("thread") or {}
            self._thread_id = thread.get("id") or thread.get("threadId")
            if not self._thread_id:
                raise RuntimeError(
                    f"thread/start did not return an id; got keys: {list(thread.keys())}"
                )
            logger.info("codex thread started: %s", self._thread_id)

        return self._thread_id

    # ------------------------------------------------------------------
    # AgentExecutor contract
    # ------------------------------------------------------------------
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        prompt = extract_message_text(context.message) or ""
        if not prompt.strip():
            await event_queue.enqueue_event(
                new_text_message("(empty prompt — nothing to do)")
            )
            return

        async with self._turn_lock:
            try:
                text = await self._run_turn(prompt)
            except AppServerError as exc:
                logger.warning("codex app-server error: %s", exc)
                await event_queue.enqueue_event(
                    new_text_message(f"[codex error] {exc}")
                )
                return
            except asyncio.TimeoutError:
                logger.warning("codex turn timed out after %.0fs", _TURN_TIMEOUT)
                await event_queue.enqueue_event(
                    new_text_message(
                        f"[codex turn timed out after {_TURN_TIMEOUT:.0f}s]"
                    )
                )
                return
            except ConnectionError as exc:
                logger.exception("codex app-server connection lost")
                # On connection loss, drop our cached app-server +
                # thread so the next turn starts fresh.
                await self._reset_app_server()
                await event_queue.enqueue_event(
                    new_text_message(f"[codex unreachable] {exc!s}")
                )
                return

        await event_queue.enqueue_event(new_text_message(text))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Best-effort interrupt of the in-flight turn.

        Race-prone (the turn may have completed between our last
        poll and this call) but the app-server treats a stale
        interrupt as a no-op, so we don't need to lock around it.
        """
        if (
            self._app_server is not None
            and self._thread_id is not None
            and self._current_turn_id is not None
        ):
            try:
                await self._app_server.request(
                    "turn/interrupt",
                    {"threadId": self._thread_id, "turnId": self._current_turn_id},
                    timeout=5.0,
                )
            except (AppServerError, asyncio.TimeoutError, ConnectionError) as exc:
                logger.debug("turn/interrupt failed (expected if turn already done): %s", exc)

    async def shutdown(self) -> None:
        """Tear down the app-server child cleanly. Idempotent."""
        await self._reset_app_server()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _run_turn(self, prompt: str) -> str:
        """Fire turn/start, accumulate deltas, return assembled text.

        Splits the AgentExecutor contract into a pure-data path so
        unit tests can drive it without standing up an A2A
        EventQueue.
        """
        thread_id = await self._ensure_thread()
        assert self._app_server is not None  # set by _ensure_thread

        state = _TurnState()
        loop = asyncio.get_running_loop()

        def on_notification(method: str, params: dict[str, Any]) -> None:
            # Codex v2 protocol notifications. Only the message stream
            # + completion + error map onto our flow today; everything
            # else (reasoning, tool exec, token usage) is logged for
            # observability but not surfaced to the A2A response.
            if method == "agent_message_delta":
                delta = params.get("delta") or params.get("text") or ""
                if delta:
                    state.deltas.append(delta)
            elif method in ("turn/completed", "turn.completed"):
                # Match either dotted or slashed form — schema is in
                # flux during the experimental phase. Also tolerate
                # both `turnId` (schema) and `id` (real binary) for
                # the params id field.
                tid = params.get("turnId") or params.get("id")
                if tid in (None, state.turn_id):
                    loop.call_soon_threadsafe(state.completed.set)
            elif method == "error_notification":
                state.error = RuntimeError(
                    params.get("message", "unknown app-server error")
                )
                loop.call_soon_threadsafe(state.completed.set)
            else:
                logger.debug("codex notification: %s %s", method, params)

        unsubscribe = self._app_server.subscribe(on_notification)
        try:
            resp = await self._app_server.request("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            })
            # Mirror the same id/threadId tolerance we have for thread/start.
            turn = resp.get("turn") or {}
            state.turn_id = turn.get("id") or turn.get("turnId")
            if not state.turn_id:
                raise RuntimeError(
                    f"turn/start did not return an id; got keys: {list(turn.keys())}"
                )
            self._current_turn_id = state.turn_id

            await asyncio.wait_for(state.completed.wait(), timeout=_TURN_TIMEOUT)
        finally:
            unsubscribe()
            self._current_turn_id = None

        if state.error:
            raise state.error
        return "".join(state.deltas)

    async def _reset_app_server(self) -> None:
        """Tear down + clear cached child. Idempotent."""
        proc = self._app_server
        self._app_server = None
        self._thread_id = None
        self._current_turn_id = None
        if proc is not None:
            try:
                await proc.close()
            except Exception:
                logger.exception("error closing codex app-server")
