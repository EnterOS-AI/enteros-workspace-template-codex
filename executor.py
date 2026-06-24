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
import json
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
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

try:
    from molecule_runtime.platform_agent_identity import set_loaded_mcp_tools
except ImportError:  # pragma: no cover - older local runtime without #3082
    # Newer runtime (core#3082) always provides this; the no-op fallback keeps
    # loaded_mcp_tools capture from breaking a turn on an older base image.
    def set_loaded_mcp_tools(_tools):  # type: ignore[misc]
        return None

from app_server import AppServerError, AppServerProcess

logger = logging.getLogger(__name__)


# Per-turn timeout. Codex turns can run minutes during heavy tool use
# (test runs, edits, web fetches). Tighter than infinite to bound
# debug-time hangs.
_TURN_TIMEOUT = 3600.0  # generous backstop ONLY. The inactivity watchdog
# below is the primary bound: an actively-progressing turn (codex still
# emitting events / tool-call I/O) must NOT be hard-killed mid-work. The
# old 600s cap killed long-but-healthy reviews while tool-calls were still
# flowing — the CR2/codex review-lane wedge (CTO 2026-06-07: extend on
# activity, don't hard-cap every turn).

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
_TURN_INACTIVITY_TIMEOUT = 300.0  # raised from 90s: long active tool-use
# (test runs, large-diff reviews) can be legitimately quiet for minutes
# between event bursts. Only a genuinely wedged channel (zero events for
# 5 min) fails the turn; healthy activity keeps extending it.

# Bootstrap RPC timeouts. ``thread/start`` is an exchange that the
# initialised child should answer in well under a second; capping it
# means a child that wedges DURING initialise gets surfaced fast
# instead of stalling the executor's first turn for 10 minutes.
_INITIALIZE_TIMEOUT = 30.0
_THREAD_START_TIMEOUT = 30.0

# MCP stdio enumeration timeouts. We list tools from each configured MCP
# server once per turn so ``loaded_mcp_tools`` reports the actual loaded
# inventory independent of whether the current turn invokes any tool.
_MCP_HANDSHAKE_TIMEOUT = 10.0
_MCP_PROTOCOL_VERSION = "2024-11-05"


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
    # MCP tool ids (``mcp__<server>__<tool>``) observed live during this turn.
    # Kept as auxiliary input to ``loaded_mcp_tools``: if inventory
    # enumeration from the config file fails, a tool that the model DID
    # invoke this turn still proves that server is live.
    mcp_tools: set = field(default_factory=set)


def _mcp_tool_id_from_item(item: dict) -> str | None:
    """Return ``mcp__<server>__<tool>`` for a codex MCP-tool-call item, else None.

    codex#3082 producer. Codex's app-server emits an ``item/completed`` (or
    bare ``item``) envelope when the model invokes an MCP tool. The exact item
    schema has shifted across 0.72→0.130 patch releases (same churn that forced
    the dual delta/completed handling above), so this reader is deliberately
    tolerant of the field-name variants seen in the wild rather than pinned to
    one shape:

      * ``type`` in {mcp_tool_call, mcpToolCall} marks the MCP-tool item; the
        server + tool names live under ``server``/``serverName`` and
        ``tool``/``toolName``/``name``.
      * Some patch builds nest these under ``item`` again — handled by the
        caller passing the inner item.

    Returns None for any non-MCP item (codex's own ``function_call`` builtin
    tools, agent messages, reasoning, etc.) so only true MCP tools land in
    ``loaded_mcp_tools`` — matching the ``mcp__`` prefix the claude-code
    producer records and the gate consumes.
    """
    if not isinstance(item, dict):
        return None
    itype = item.get("type") or ""
    if itype not in ("mcp_tool_call", "mcpToolCall"):
        return None
    server = item.get("server") or item.get("serverName") or ""
    tool = item.get("tool") or item.get("toolName") or item.get("name") or ""
    if not server or not tool:
        return None
    return f"mcp__{server}__{tool}"


def _codex_config_path() -> Path:
    """Return the codex config file the running CLI reads from.

    Honors ``$CODEX_HOME`` so tests and multi-home deployments can
    isolate the file without touching the real ``~/.codex``.
    """
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~")
    return Path(home) / ".codex" / "config.toml"


def _read_codex_mcp_servers(config_path: Path | None = None) -> dict[str, dict]:
    """Return ``{server_name: spec}`` from ``[mcp_servers]`` in config.toml.

    Returns an empty dict when the file is missing, unreadable, or
    contains no MCP server tables. This is the "no-loaded" case that
    keeps the gate degraded rather than guessing a static tool list.
    """
    path = config_path or _codex_config_path()
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return {}
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    return {
        name: spec
        for name, spec in servers.items()
        if isinstance(spec, dict)
    }


async def _send_mcp_message(
    proc: asyncio.subprocess.Process, msg: dict[str, Any]
) -> None:
    """Write one newline-delimited JSON-RPC message to the MCP server."""
    assert proc.stdin is not None
    line = json.dumps(msg, separators=(",", ":")) + "\n"
    proc.stdin.write(line.encode("utf-8"))
    await proc.stdin.drain()


async def _recv_mcp_response(
    proc: asyncio.subprocess.Process, *, expected_id: int, timeout: float
) -> dict[str, Any] | None:
    """Read JSON-RPC responses until ``expected_id`` arrives.

    Drops unsolicited notifications/requests. Returns ``None`` on EOF or
    timeout so callers degrade gracefully.
    """
    assert proc.stdout is not None
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return None
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        if not raw:
            return None
        try:
            msg = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and msg.get("id") == expected_id:
            return msg


async def _list_tools_from_mcp_server(
    command: str,
    args: list[str],
    env: dict[str, str],
) -> list[str]:
    """Return raw tool names advertised by one MCP server.

    Performs a minimal stdio JSON-RPC handshake (initialize +
    notifications/initialized + tools/list). Any failure — missing
    binary, handshake timeout, unexpected response — returns an empty
    list so a flaky or misconfigured server is treated as "not loaded"
    rather than crashing the turn.
    """
    cmd = [command, *(args or [])]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except Exception as exc:
        logger.debug("MCP server spawn failed for %r: %s", command, exc)
        return []

    try:
        await _send_mcp_message(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "molecule-runtime-codex",
                        "version": "0.1.0",
                    },
                },
            },
        )
        init_resp = await _recv_mcp_response(
            proc, expected_id=1, timeout=_MCP_HANDSHAKE_TIMEOUT
        )
        if not isinstance(init_resp, dict) or "result" not in init_resp:
            return []

        await _send_mcp_message(
            proc,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        await _send_mcp_message(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )
        tools_resp = await _recv_mcp_response(
            proc, expected_id=2, timeout=_MCP_HANDSHAKE_TIMEOUT
        )
        if not isinstance(tools_resp, dict) or "result" not in tools_resp:
            return []
        tools = tools_resp["result"].get("tools")
        if not isinstance(tools, list):
            return []
        return [
            str(t["name"])
            for t in tools
            if isinstance(t, dict) and isinstance(t.get("name"), str)
        ]
    except Exception as exc:
        logger.debug("MCP tool enumeration failed for %r: %s", command, exc)
        return []
    finally:
        try:
            if proc.returncode is None:
                proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass


async def extract_loaded_mcp_tools(
    config_path: Path | None = None,
) -> list[str]:
    """Return the loaded MCP inventory as ``mcp__<server>__<tool>`` ids.

    Mirrors the google-adk #3082 inventory fix: instead of only the
    tools invoked during the current turn, enumerate the actually-loaded
    tool declarations from each MCP server configured in codex's native
    ``~/.codex/config.toml``. Empty/no-loaded stays degraded (empty list).
    """
    servers = _read_codex_mcp_servers(config_path)
    if not servers:
        return []

    result: list[str] = []
    seen: set[str] = set()
    for name, spec in servers.items():
        command = spec.get("command")
        if not isinstance(command, str) or not command:
            continue
        args = spec.get("args")
        if not isinstance(args, list):
            args = []
        # Server env overrides the parent process env (matches how codex
        # spawns its MCP children).
        spec_env = spec.get("env")
        server_env = {
            **os.environ,
            **(spec_env if isinstance(spec_env, dict) else {}),
        }
        raw_tools = await _list_tools_from_mcp_server(command, args, server_env)
        for tool in raw_tools:
            tid = f"mcp__{name}__{tool}"
            if tid not in seen:
                seen.add(tid)
                result.append(tid)
    return sorted(result)


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
        # Cache for the enumerated MCP inventory. ``None`` means "not yet
        # successfully enumerated"; an empty set means "enumerated and
        # no MCP tools are loaded". Once non-empty, the inventory is
        # stable for the lifetime of the executor.
        self._loaded_mcp_tools: set[str] | None = None

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

    async def _ensure_loaded_mcp_tools(self) -> set[str]:
        """Return the loaded MCP inventory, enumerating it once per session.

        The inventory is read from codex's native ``~/.codex/config.toml``
        by spawning each configured MCP server and calling ``tools/list``.
        A non-empty result is cached; an empty result is NOT cached so a
        slow-to-start or transiently-failing server is retried next turn
        rather than permanently cached as degraded.
        """
        if self._loaded_mcp_tools is not None:
            return self._loaded_mcp_tools
        tools = await extract_loaded_mcp_tools()
        if tools:
            self._loaded_mcp_tools = set(tools)
        return self._loaded_mcp_tools or set()

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
                # Approach B (chat-priority): a message arriving mid-turn would
                # otherwise be steered into a long in-flight turn (e.g. an
                # autonomous tick) and the agent could keep doing self-directed
                # work without surfacing a reply — leaving a canvas user staring
                # at the placeholder. Prepend an explicit directive so codex
                # answers the waiting requester promptly within the steered turn.
                steer_prompt = (
                    "[A new message just arrived while you are mid-task. If it "
                    "is from the user, reply to them promptly via the "
                    "send_message_to_user tool (a brief acknowledgement or a "
                    "direct answer) before continuing your current work — do "
                    "not leave them waiting. If it is a delegated/peer request, "
                    "address it as appropriate. Their message follows:]\n\n"
                    + prompt
                )
                await self._app_server.request(
                    "turn/steer",
                    {
                        "threadId": self._thread_id,
                        "input": self._build_turn_input(steer_prompt, attached),
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
                        "Got your message \u2014 I\u2019m mid-task right now "
                        "and will reply here shortly."
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
                # Drop the cached app-server + thread so the NEXT turn
                # starts fresh. Without this the stale app-server child
                # stays cached and every subsequent turn re-times-out
                # until container restart — the CR2/codex review-lane
                # 600s wedge (molecule-ai/internal#653, #781). Mirrors
                # the ConnectionError path below.
                await self._reset_app_server()
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

        # codex#3082 / #142: report the actual loaded MCP inventory, not just
        # the tools invoked this turn. Enumerate each configured MCP server
        # from ~/.codex/config.toml and merge with any tools observed live.
        loaded_tools = await self._ensure_loaded_mcp_tools()

        state = _TurnState()
        state.mcp_tools.update(loaded_tools)
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
                # codex#3082: record MCP tool calls so the heartbeat can report
                # loaded_mcp_tools (proves the management MCP's tools are LIVE,
                # not merely declared in config.toml). No-op for non-MCP items.
                tid = _mcp_tool_id_from_item(item)
                if tid:
                    state.mcp_tools.add(tid)
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
            # codex#142 / #3082: publish the loaded MCP inventory (enumerated
            # from ~/.codex/config.toml) plus any tools invoked live this turn.
            # A healthy concierge with the management MCP loaded reports
            # create_workspace even on turns where the model did not call it,
            # so the platform online/degraded gate doesn't fail-close. An
            # empty list is still a meaningful "a turn completed" signal.
            # Bookkeeping only; never let it raise into the turn result.
            try:
                set_loaded_mcp_tools(sorted(state.mcp_tools))
            except Exception:  # noqa: BLE001
                logger.debug("loaded_mcp_tools capture skipped", exc_info=True)

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

        - ``_TURN_INACTIVITY_TIMEOUT`` (300 s) — max gap between events.
          A healthy turn emits ``codex/event/*`` notifications
          continuously; a wedged channel emits zero. If the activity
          counter does not advance for this long, we raise
          ``asyncio.TimeoutError`` instead of waiting the full
          ``_TURN_TIMEOUT``. This is the safety net for the 2026-05-18
          production wedge: the executor would otherwise hold the
          turn-lock for 10 minutes per stuck request, masking the
          real channel failure.

        - ``_TURN_TIMEOUT`` (3600 s) — hard upper bound for total turn
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
