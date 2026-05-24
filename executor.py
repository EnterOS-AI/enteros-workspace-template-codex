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
from molecule_runtime.executor_helpers import (
    extract_attached_files,
    extract_message_text,
)
try:
    from molecule_runtime.attachment_vision import append_image_descriptions
except ModuleNotFoundError:  # pragma: no cover - older local runtime
    async def append_image_descriptions(text, files):
        return text

from app_server import AppServerError, AppServerProcess

logger = logging.getLogger(__name__)


# Per-turn timeout. Codex turns can run minutes during heavy tool use
# (test runs, edits, web fetches). Tighter than infinite to bound
# debug-time hangs.
_TURN_TIMEOUT = 600.0

# Inactivity watchdog: cap the gap BETWEEN events from codex. A healthy
# turn emits frequent ``codex/event/*`` notifications (token deltas,
# tool I/O, reasoning markers) — minutes-long gaps are themselves
# evidence the channel is wedged, not work-in-progress. Smaller than
# ``_TURN_TIMEOUT`` so a stuck child surfaces an error promptly to the
# user instead of holding the lock for 10 minutes.
#
# Tuned from the production wedge:
#   - Healthy fresh turn (gpt-5.5, no tool use): 2-3 s end-to-end.
#   - Heavy tool-use turn: deltas every few seconds at most.
#   - Wedged channel: zero events, zero rollout bytes for the full
#     ``_TURN_TIMEOUT`` window. The watchdog catches that in 90 s
#     instead of 600 s, and prints a diagnostic message.
_TURN_INACTIVITY_TIMEOUT = 90.0

# Bootstrap RPC timeouts. ``thread/start`` is an exchange that the
# initialised child should answer in well under a second; capping it
# means a child that wedges DURING initialise gets surfaced fast
# instead of stalling the executor's first turn for 10 minutes.
_INITIALIZE_TIMEOUT = 30.0
_THREAD_START_TIMEOUT = 30.0


@dataclass
class _TurnState:
    """Mutable state accumulated during one turn lifecycle.

    Owned by the running ``_run_turn`` invocation; the notification
    subscriber appends to it under ``_turn_lock``.

    ``activity`` is bumped on every notification the subscriber sees,
    even ones we don't materially care about (debug-level events,
    reasoning markers, tool I/O). It's the heartbeat the inactivity
    watchdog reads — if the watchdog ticks and ``activity`` has not
    advanced since the last tick, the channel is wedged and we surface
    a diagnostic error.
    """
    deltas: list[str] = field(default_factory=list)
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    error: Exception | None = None
    turn_id: str | None = None
    activity: int = 0


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
        self._pending_attached_files: list[dict[str, str]] = []

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
            # Bounded handshake — a child wedged on initialize (rare but
            # observed when stdio fights with a debug-attached pty)
            # would otherwise stall the FIRST turn for the full
            # _DEFAULT_REQUEST_TIMEOUT (10 minutes).
            await asyncio.wait_for(
                self._app_server.initialize(client_info={
                    "name": "molecule-runtime-codex",
                    "version": "0.1.0",
                }),
                timeout=_INITIALIZE_TIMEOUT,
            )
            logger.info("codex app-server child initialized")

        if self._thread_id is None:
            params: dict[str, Any] = {}
            if self._config.model:
                params["model"] = self._config.model
            if self._config.system_prompt:
                params["developerInstructions"] = self._config.system_prompt
            # Workspace agents can't prompt a human, so approval policy
            # must be `never`.
            #
            # Sandbox mode: `danger-full-access` (no bwrap at all).
            #
            # Why not `workspace-write` + `network_access: True`?
            # That config is the correct one philosophically, but on
            # the deployed codex-cli 0.130.0 binary it tries to bring
            # up a private network namespace via bwrap `--unshare-net`,
            # which fails inside our docker container with
            #   `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`
            # because the container doesn't carry `CAP_NET_ADMIN`. The
            # capability could be granted via docker `--cap-add NET_ADMIN`
            # in the provisioner, but that's a controlplane-side change.
            #
            # Until the provisioner adds the capability, sandboxPolicy
            # `danger-full-access` bypasses bwrap entirely → no netns
            # setup → network works → agent can `git clone` / `curl`
            # Gitea + GitHub for PR review and code fetches. The
            # workspace agent runs as uid-1000 inside a per-tenant EC2
            # so blast radius is bounded to the workspace's own
            # filesystem + that one EC2's network identity.
            #
            # Tracked: file follow-up in molecule-controlplane to add
            # NET_ADMIN to the codex container run args, then revert
            # this to workspace-write + network_access:True.
            params["approvalPolicy"] = "never"
            params["sandboxPolicy"] = {"mode": "danger-full-access"}

            resp = await self._app_server.request(
                "thread/start", params, timeout=_THREAD_START_TIMEOUT,
            )
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
        text = extract_message_text(context.message) or ""
        # Phase 1 file-only message support (a1ea2200 archaeology — chloe-dong
        # PDF-only canary 2026-05-20 01:04:27Z surfaced the opaque
        # "(empty prompt — nothing to do)" reply). Mirror the claude-code
        # reference impl (claude_sdk_executor.py:644-650): surface attached
        # files to codex as a manifest in the prompt — codex reads files
        # through its own tools by path. Phase 2 will wire actual
        # file-content forwarding via codex's input parts.
        attached = extract_attached_files(context.message)
        if attached:
            text = await append_image_descriptions(text, attached)
            manifest = "\n\nAttached files:\n" + "\n".join(
                f"- {f['name']} ({f['mime_type'] or 'unknown type'}) at {f['path']}"
                for f in attached
            )
            text = (text + manifest) if text.strip() else manifest.lstrip()
        if not text.strip():
            # Truly empty — actionable per
            # feedback_surface_actionable_failure_reason_to_user.
            await event_queue.enqueue_event(
                new_text_message(
                    "Your message was empty. Please send text or a file "
                    "with instructions."
                )
            )
            return
        prompt = text

        # Push parity with claude-code: when a new message arrives while
        # a turn is already in flight, inject it into the active turn
        # via codex's `turn/steer` RPC instead of blocking on the lock
        # for ~minutes until the prior turn finishes. This is the
        # documented v2 codex app-server protocol primitive — see
        # codex-rs/app-server/README.md§Steer-an-active-turn — and
        # gives codex true mid-turn push semantics matching the
        # `notifications/claude/channel` path Claude Code uses.
        #
        # The agent then sees the new prompt as additional input in the
        # active turn's context. Per the molecule MCP server's
        # instructions string, the agent replies via send_message_to_user
        # (canvas) or delegate_task (peer) — the platform's reply path
        # is tool-based, not the A2A response shape — so this execute()
        # returning a placeholder is correct: the actual reply lands
        # via the tool-call route, not through this event_queue.
        if (
            self._turn_lock.locked()
            and self._app_server is not None
            and self._thread_id is not None
            and self._current_turn_id is not None
        ):
            try:
                await self._app_server.request(
                    "turn/steer",
                    {
                        "threadId": self._thread_id,
                        "input": self._build_turn_input(prompt, attached),
                        "expectedTurnId": self._current_turn_id,
                    },
                    timeout=5.0,
                )
                logger.info(
                    "codex push: steered into active turn %s",
                    self._current_turn_id,
                )
                # Status placeholder for the A2A response. The peer or
                # canvas wrapper sees this; the agent's substantive
                # reply comes via send_message_to_user / delegate_task
                # MCP tool calls within the steered turn's response.
                await event_queue.enqueue_event(
                    new_text_message(
                        "[steered into in-flight turn — agent will reply "
                        "via send_message_to_user / delegate_task]"
                    )
                )
                return
            except (AppServerError, asyncio.TimeoutError) as exc:
                # Steer failed — common causes:
                #   - ActiveTurnNotSteerable (review/manual-compact turn)
                #   - expectedTurnId mismatch (turn ended between our
                #     locked-check and the steer request)
                #   - app-server transport hiccup
                # Fall through to the lock-and-wait path so the message
                # still gets processed, just as a queued new turn.
                logger.debug(
                    "codex turn/steer failed (%s) — falling through to new-turn path",
                    exc,
                )

        async with self._turn_lock:
            try:
                self._pending_attached_files = attached
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
            except RuntimeError as exc:
                # Surfaced from `state.error` in `_run_turn` — codex emitted
                # an `error` notification (typically an upstream HTTP failure
                # from the model provider, e.g. `unexpected status 401
                # Unauthorized`). Wrapping with the same `[codex error]`
                # prefix the AppServerError path uses keeps the canvas-side
                # behavior consistent: a clear inline message instead of a
                # bare JSON-RPC -32603 leak from the a2a-sdk top-level
                # handler.
                logger.warning("codex turn surfaced error: %s", exc)
                await event_queue.enqueue_event(
                    new_text_message(f"[codex error] {exc}")
                )
                return
            finally:
                self._pending_attached_files = []

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
            # Codex emits notifications in two schemas the executor must
            # handle simultaneously — the deployed CLI version changed
            # the wire protocol without bumping the JSON-RPC version, so
            # both formats appear in production.
            #
            # **codex 0.72 (legacy)** — single namespace `codex/event/<type>`
            # JSON-RPC method, event payload under `params.msg` and
            # `params.msg.type` carrying the event-type tag.
            #
            # **codex 0.130 (current deployed binary)** — top-level method
            # names: `item/agentMessage/delta`, `item/completed`,
            # `turn/completed`, `thread/started`, etc. Verified live via
            # container-side instrumentation 2026-05-22 against
            # codex-cli 0.130.0 deployed image: turns completed cleanly
            # at the codex side (delta + completed + turn/completed all
            # fired), but the executor matched none of the 0.130 method
            # names so `state.deltas` stayed empty and
            # `state.completed.set()` was never called. The inactivity
            # watchdog then fired 90s after codex's last notification,
            # producing the "wedged: no events for 90s (deltas=0)"
            # symptom even though codex delivered the response.
            #
            # Activity bump: every notification (matched or unmatched)
            # is the heartbeat for the inactivity watchdog. We bump
            # before the early returns so even ignored bare-method
            # events keep the channel "alive".
            state.activity += 1
            if method == "error":
                # Bare-method `error` notifications (parallel schema)
                # carry the error payload under `params.error`. These
                # often duplicate a `codex/event/stream_error` —
                # surface only the final non-retry one so the operator
                # sees the real failure.
                err = params.get("error") or {}
                if not params.get("willRetry"):
                    state.error = RuntimeError(
                        str(err.get("message") or "unknown codex error")
                    )
                    loop.call_soon_threadsafe(state.completed.set)
                return

            # codex 0.130 schema — top-level methods. Handled BEFORE the
            # `codex/event/` filter so they take priority on the deployed
            # binary; the 0.72 fallback below remains for any future
            # downgrade or vendor variant that re-emits the old shape.
            if method == "item/agentMessage/delta":
                # Delta text under `params.delta` per codex-rs/app-server
                # schema; fall back to `params.text` for variants and to
                # the `params.item` envelope some 0.130.x patch releases
                # used while the schema was settling.
                delta = (
                    params.get("delta")
                    or params.get("text")
                    or (params.get("item") or {}).get("delta")
                    or (params.get("item") or {}).get("text")
                    or ""
                )
                if delta:
                    state.deltas.append(delta)
                return
            if method == "item/completed":
                # Final assembled message — recover the full text when
                # delta streaming was skipped (non-OpenAI backends) or
                # when we missed a delta chunk. Idempotent dedupe so a
                # streaming turn doesn't double the text.
                item = params.get("item") or {}
                if item.get("type") in (
                    "agent_message",
                    "assistant_message",
                    "agentMessage",
                    "assistantMessage",
                ):
                    whole = item.get("message") or item.get("text") or ""
                    if whole and whole not in state.deltas:
                        state.deltas.append(whole)
                return
            if method == "turn/completed":
                # 0.130's equivalent of `task_complete`. Codex emits
                # this AFTER the last `item/completed`, so by the time
                # we set state.completed.set the deltas list is already
                # populated. If the streaming missed entirely and we
                # have no deltas, `last_agent_message` (when present
                # in params) is the recovery.
                last = params.get("last_agent_message") or params.get("message") or ""
                if last and last not in state.deltas:
                    state.deltas.append(last)
                loop.call_soon_threadsafe(state.completed.set)
                return

            if not method.startswith("codex/event/"):
                logger.debug("codex notification: %s %s", method, params)
                return

            msg = params.get("msg") or {}
            mtype = msg.get("type", "")
            if mtype == "agent_message_delta":
                delta = msg.get("delta") or msg.get("text") or ""
                if delta:
                    state.deltas.append(delta)
            elif mtype == "agent_message":
                # Whole-message form: codex emits this when the model
                # response wasn't streamed as chunks (most non-OpenAI
                # backends). Append as a single delta so the assembled
                # string is complete even without `_delta` fragments.
                whole = msg.get("message") or msg.get("text") or ""
                if whole:
                    state.deltas.append(whole)
            elif mtype == "task_complete":
                # task_complete carries `last_agent_message` — when
                # the model returned a single message and skipped
                # streaming, this is the only place the text shows
                # up. Treat it as a final delta if we haven't seen
                # an `agent_message` already (idempotent dedupe).
                last = msg.get("last_agent_message") or ""
                if last and last not in state.deltas:
                    state.deltas.append(last)
                loop.call_soon_threadsafe(state.completed.set)
            elif mtype == "error":
                state.error = RuntimeError(
                    str(msg.get("message") or "unknown codex error")
                )
                loop.call_soon_threadsafe(state.completed.set)
            elif mtype == "stream_error":
                # Retry signal — codex retries internally. Log it
                # but don't surface; the final `error` (or
                # task_complete) will resolve the turn.
                logger.info(
                    "codex stream_error (will retry): %s",
                    msg.get("message", "")
                )
            else:
                logger.debug("codex event: %s %s", mtype, msg)

        unsubscribe = self._app_server.subscribe(on_notification)
        try:
            resp = await self._app_server.request("turn/start", {
                "threadId": thread_id,
                "input": self._build_turn_input(prompt, self._pending_attached_files),
            })
            # Mirror the same id/threadId tolerance we have for thread/start.
            turn = resp.get("turn") or {}
            state.turn_id = turn.get("id") or turn.get("turnId")
            if not state.turn_id:
                raise RuntimeError(
                    f"turn/start did not return an id; got keys: {list(turn.keys())}"
                )
            self._current_turn_id = state.turn_id

            await self._await_turn_completion(state)
        finally:
            unsubscribe()
            self._current_turn_id = None

        if state.error:
            raise state.error
        return "".join(state.deltas)

    def _build_turn_input(
        self,
        prompt: str,
        attached: list[dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        """Build codex app-server input items from text plus image attachments."""
        items: list[dict[str, str]] = [{"type": "text", "text": prompt}]
        for file in attached or []:
            if (file.get("mime_type") or "").startswith("image/") and file.get("path"):
                items.append({"type": "localImage", "path": file["path"]})
        return items

    async def _await_turn_completion(self, state: _TurnState) -> None:
        """Wait for turn completion with two stacked timeouts.

        Stacked bounds:

        - ``_TURN_INACTIVITY_TIMEOUT`` (90 s) — max gap between events.
          A healthy turn emits ``codex/event/*`` notifications
          continuously; a wedged channel emits zero. If the activity
          counter does not advance for this long, we raise
          ``asyncio.TimeoutError`` instead of waiting the full
          ``_TURN_TIMEOUT``. This is the safety net for the 2026-05-18
          production wedge: the executor would otherwise hold the
          turn-lock for 10 minutes per stuck request, masking the
          real channel failure.

        - ``_TURN_TIMEOUT`` (600 s) — hard upper bound for total turn
          duration even if events keep arriving. Preserves the
          previous-generation bound for legitimately-long tool-use
          turns (test runs, etc.).

        The watchdog runs in 5 s ticks. Each tick:
          1. If the completion event is set, return.
          2. If the activity counter has not changed since the last
             tick AND the inactivity window has elapsed, raise
             TimeoutError.
          3. If the total elapsed time exceeds ``_TURN_TIMEOUT``, raise
             TimeoutError.
        """
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        last_seen_activity = state.activity
        last_activity_at = started_at
        tick = 5.0

        while True:
            try:
                await asyncio.wait_for(state.completed.wait(), timeout=tick)
                return
            except asyncio.TimeoutError:
                pass

            now = loop.time()
            if state.activity != last_seen_activity:
                last_seen_activity = state.activity
                last_activity_at = now

            if now - last_activity_at >= _TURN_INACTIVITY_TIMEOUT:
                logger.warning(
                    "codex turn %s wedged: no events for %.0fs "
                    "(deltas=%d) — failing turn",
                    state.turn_id,
                    now - last_activity_at,
                    len(state.deltas),
                )
                raise asyncio.TimeoutError(
                    f"codex emitted no events for "
                    f"{_TURN_INACTIVITY_TIMEOUT:.0f}s — channel wedged"
                )
            if now - started_at >= _TURN_TIMEOUT:
                raise asyncio.TimeoutError(
                    f"codex turn exceeded total budget "
                    f"{_TURN_TIMEOUT:.0f}s"
                )

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
