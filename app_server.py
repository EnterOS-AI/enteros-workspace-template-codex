"""Async JSON-RPC client for `codex app-server`.

Spawns `codex app-server` as a long-lived child process, frames messages
as NDJSON over stdio, correlates request/response by id, and dispatches
unsolicited server notifications to registered subscribers.

The executor uses this primitive to hold one thread per workspace
session and fire `turn/start` per A2A message — see executor.py for
the protocol-level usage.

Framing: `codex app-server` uses newline-delimited JSON, NOT
LSP-style content-length headers. Validated 2026-05-02 against
codex-cli 0.72.0:

    $ echo '{"jsonrpc":"2.0","id":1,"method":"initialize",...}' | codex app-server
    {"id":1,"result":{"userAgent":"codex_cli_rs/0.72.0 …"}}

Concurrency model: a single asyncio Task drains stdout line-by-line,
parses each JSON object, and routes it to either a pending request
future (matched by id) or the notification subscriber list. Writes are
serialized through an asyncio.Lock — concurrent request() calls are
safe but ordering is not guaranteed at the protocol level (the
app-server's request id is what matters, not write order).

Errors: any exception in the reader task fails ALL pending requests
with that exception, prevents new request()s from succeeding, and
surfaces the original cause in the close() return value. Designed so a
mid-flight stdout pipe break doesn't silently hang request() callers.

Failure modes the reader explicitly handles (see ``_read_loop`` and
``_watch_child``):

1. Reader raises (decode error → wraps as ConnectionError; cancelled →
   propagates). Pending futures fail with the captured exception.
2. Reader exits cleanly because stdout reached EOF — the child closed
   the pipe (crashed, exited, or got buggy and stopped writing). We
   treat EOF the same as an explicit error: ``_reader_exc`` is set and
   pending futures are failed with ``ConnectionError("app-server stdout
   closed (EOF) — child exited or stopped writing")``. Without this,
   any pending ``request()`` would hang for the full request timeout
   (10 min) even though the channel is irrecoverably dead.
3. Child process exits (e.g. SIGKILL, segfault, OOM kill) while the
   reader is mid-line. ``_watch_child`` awaits ``proc.wait()`` and on
   completion fails all pending futures with ``ConnectionError("app-
   server child exited with code …")``. Covers the case where the
   reader is parked in ``readuntil`` and the OS reaps the child before
   the pipe drains.

Both paths converge: any request still in ``_pending`` when the
channel goes dead receives a ConnectionError, never a silent
infinite wait. Both paths set ``_reader_exc`` so subsequent
``request()`` calls fail fast at the precondition check rather than
queueing a future that will never resolve.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# Per-request timeout. Codex turns can run minutes during heavy
# tool-use, so this is generous. Tighter than infinite to bound
# debug-time hangs when the app-server gets wedged.
_DEFAULT_REQUEST_TIMEOUT = 600.0

# Graceful-shutdown grace period before SIGKILL. App-server's own
# shutdown is fast (<1s), so this is a fallback for hung children.
_SHUTDOWN_TIMEOUT = 5.0

NotificationCallback = Callable[[str, dict[str, Any]], None]
InboundRequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]


class AppServerError(RuntimeError):
    """Raised when the app-server returns a JSON-RPC error response.

    The wrapped JSON-RPC error object is exposed via ``.payload`` for
    callers that want to inspect ``code`` / ``data`` fields.
    """

    def __init__(self, message: str, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.payload = payload or {}


class AppServerProcess:
    """Long-lived `codex app-server` child plus async JSON-RPC client.

    Typical lifecycle:

        proc = await AppServerProcess.start()
        await proc.initialize(client_info={...})

        unsub = proc.subscribe(on_notification)
        try:
            resp = await proc.request("thread/start", {...})
            …
        finally:
            unsub()

        await proc.close()

    Not safe to share across asyncio loops.
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        request_timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ):
        self._proc = process
        self._request_timeout = request_timeout
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscribers: list[NotificationCallback] = []
        # Caller-registered handlers for server-initiated requests
        # (e.g. mcpServer/elicitation/request). Maps method name to an
        # async callable that returns the JSON-RPC result payload.
        # Unhandled methods fall through to the default policy in
        # ``_dispatch``.
        self._inbound_handlers: dict[str, InboundRequestHandler] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._watcher_task: asyncio.Task[None] | None = None
        self._closed = False
        self._reader_exc: BaseException | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    async def start(
        cls,
        *,
        executable: str = "codex",
        args: tuple[str, ...] = ("app-server",),
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> "AppServerProcess":
        """Spawn `codex app-server` as a stdio-piped child."""
        proc_env = {**os.environ, **(env or {})}
        # `limit` controls the underlying asyncio.StreamReader buffer for
        # stdout/stderr. Default is 64 KB (asyncio.streams._DEFAULT_LIMIT),
        # which the codex app-server can exceed on a single JSON-RPC line
        # — e.g. a Code-Reviewer agent's review payload that embeds a full
        # PR diff. When a line is bigger than the limit, _read_loop's
        # `async for raw in self._proc.stdout` raises
        # `ValueError('Separator is found, but chunk is longer than limit')`
        # (or '... not found, and chunk exceed the limit') and the reader
        # task dies — every pending request then hangs until timeout, and
        # CR2/Researcher become permanently unable to complete codex turns.
        # 10 MB is well above the largest review payload observed
        # (canvas screenshot 2026-05-23) while still bounded.
        proc = await asyncio.create_subprocess_exec(
            executable,
            *args,
            limit=10 * 1024 * 1024,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
            cwd=cwd,
        )
        instance = cls(proc)
        instance._reader_task = asyncio.create_task(
            instance._read_loop(), name="codex-app-server-stdout"
        )
        instance._stderr_task = asyncio.create_task(
            instance._stderr_loop(), name="codex-app-server-stderr"
        )
        instance._watcher_task = asyncio.create_task(
            instance._watch_child(), name="codex-app-server-watcher"
        )
        return instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def initialize(self, *, client_info: dict[str, Any]) -> dict[str, Any]:
        """Send the `initialize` handshake. Must be called before other RPCs.

        Per the codex app-server protocol contract (codex-rs/app-server/README.md
        + codex-rs/app-server-protocol/schema/json/ClientNotification.json), a
        client MUST send an ``initialized`` notification (no id) AFTER receiving
        the ``initialize`` response and BEFORE sending any further request.
        Otherwise codex returns ``Not initialized`` errors on subsequent calls.

        codex-cli 0.72.x was permissive about a missing ``initialized`` —
        ``thread/start`` worked anyway. 0.130.0 (current production image) is
        strict: every post-initialize RPC silently hangs / rejects until the
        notification arrives. Reproduced live 2026-05-24 against
        agents-team prod CR2 + Researcher (workspaces 4e817f43… and
        712b5600…). See internal#659 P1#1.
        """
        result = await self.request("initialize", {"clientInfo": client_info})
        # Spec requires this notification to acknowledge the initialize
        # handshake. Without it, codex 0.130+ wedges every subsequent request.
        await self.notify("initialized")
        return result

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no id, no response).

        Raises ConnectionError if the channel is closed or the reader
        has marked the channel dead.
        """
        if self._closed:
            raise ConnectionError("app-server is closed")
        if self._reader_exc is not None:
            raise ConnectionError(
                f"app-server reader failed: {self._reader_exc!r}"
            ) from self._reader_exc
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._write_message(message)

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and await its response.

        Raises:
            AppServerError: app-server returned a JSON-RPC error.
            asyncio.TimeoutError: response not received in time.
            ConnectionError: child process exited or stdio broken.
        """
        if self._closed:
            raise ConnectionError("app-server is closed")
        if self._reader_exc is not None:
            raise ConnectionError(
                f"app-server reader failed: {self._reader_exc!r}"
            ) from self._reader_exc

        request_id = self._next_id
        self._next_id += 1

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params

        # Re-check after registering the future: the reader could have
        # marked the channel dead between the precondition check and
        # the future being added to _pending. Without this, a future
        # added strictly after _mark_dead ran would not get failed.
        if self._reader_exc is not None:
            self._pending.pop(request_id, None)
            raise ConnectionError(
                f"app-server reader failed: {self._reader_exc!r}"
            ) from self._reader_exc

        try:
            await self._write_message(message)
            return await asyncio.wait_for(
                future, timeout=timeout if timeout is not None else self._request_timeout
            )
        finally:
            self._pending.pop(request_id, None)

    def subscribe(self, callback: NotificationCallback) -> Callable[[], None]:
        """Register a callback for unsolicited server notifications.

        The callback receives `(method, params)` for every
        `JSONRPCNotification` (a JSON-RPC message with no `id`).
        Returns an unsubscribe callable.

        Subscribers are called synchronously from the reader loop —
        keep them fast. Push slow work onto an asyncio.Queue if you
        need to do anything substantial.
        """
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def set_inbound_request_handler(
        self, method: str, handler: InboundRequestHandler | None
    ) -> None:
        """Register/clear a handler for a server-initiated request method.

        Codex's app-server can send JSON-RPC requests to the client
        (e.g. `mcpServer/elicitation/request`). Without a handler, the
        request hangs the active turn until codex's 90-second
        per-event watchdog fires and fails the turn.

        Handler is an async callable `(method, params) -> result`. Its
        return value becomes the `result` of the JSON-RPC response.
        Exceptions are caught and surfaced as JSON-RPC error -32000.

        Pass `handler=None` to remove a previously-registered handler;
        the default policy in ``_dispatch`` then applies.
        """
        if handler is None:
            self._inbound_handlers.pop(method, None)
        else:
            self._inbound_handlers[method] = handler

    async def close(self) -> int | None:
        """Close stdio, wait for child exit, return its exit code.

        Idempotent. Safe to call from finally blocks.
        """
        if self._closed:
            return self._proc.returncode
        self._closed = True

        # Close stdin first to signal graceful shutdown — codex
        # app-server exits cleanly on EOF.
        if self._proc.stdin and not self._proc.stdin.is_closing():
            try:
                self._proc.stdin.close()
                await self._proc.stdin.wait_closed()
            except Exception:
                pass

        # Cancel reader tasks; they should exit on stdout EOF anyway.
        # The watcher task drops out naturally once proc.wait() returns
        # below, but cancelling it here is safe (and idempotent) — it
        # avoids a stray pending task in pathological cases where
        # SIGKILL doesn't actually reap the child.
        for task in (self._reader_task, self._stderr_task, self._watcher_task):
            if task and not task.done():
                task.cancel()

        # Fail any pending requests so awaiters don't hang.
        exc = ConnectionError("app-server closed")
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

        try:
            return await asyncio.wait_for(self._proc.wait(), timeout=_SHUTDOWN_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("codex app-server did not exit cleanly; sending SIGKILL")
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
            try:
                return await self._proc.wait()
            except Exception:
                return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _write_message(self, message: dict[str, Any]) -> None:
        if self._proc.stdin is None or self._proc.stdin.is_closing():
            raise ConnectionError("app-server stdin closed")
        line = json.dumps(message, separators=(",", ":")) + "\n"
        async with self._write_lock:
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        """Drain stdout line-by-line, route messages by id.

        Three exit conditions, all of which mark the channel dead and
        fail every pending request:

        1. Exception during read / parse — capture, set ``_reader_exc``,
           propagate to the task.
        2. Cancellation (close() in progress) — re-raise without
           touching pending state; ``close()`` handles those.
        3. EOF on stdout (``async for`` completes normally) — the child
           closed the pipe. Treat the SAME as a fatal exception: set
           ``_reader_exc`` to a ConnectionError, fail pending futures.

        (3) is the production wedge. Pre-fix the loop returned cleanly
        on EOF, ``_reader_exc`` stayed None, and any future a caller
        registered after EOF would wait the full request timeout (10
        minutes) before timing out — looking exactly like the
        ``message/send`` 60 s curl wedge with 0 bytes received that
        prod-Reviewer/Researcher hit on the 2026-05-18 probe.
        """
        assert self._proc.stdout is not None
        try:
            async for raw in self._proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("non-JSON line from app-server: %r", line[:200])
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._mark_dead(exc)
            raise
        else:
            # Normal exit = stdout reached EOF. Channel is dead even
            # though no exception fired. Without this branch, pending
            # requests would silently wait out the full request timeout.
            self._mark_dead(
                ConnectionError(
                    "app-server stdout closed (EOF) — child exited or "
                    "stopped writing"
                )
            )

    async def _watch_child(self) -> None:
        """Reap the child and fail pending requests if it exits.

        ``_read_loop`` catches stdout EOF, but a child that segfaults /
        is OOM-killed / SIGKILLed may have its stdout drained before
        the reader notices, or the reader may be parked in
        ``readuntil`` while ``wait()`` returns first. This watcher is
        the second-chance fail-fast: any pending future not already
        failed by the reader gets one here.
        """
        try:
            rc = await self._proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("child wait() failed")
            return
        if self._closed:
            return
        # Reader may have already fired _mark_dead; this is idempotent.
        self._mark_dead(
            ConnectionError(f"app-server child exited with code {rc}")
        )

    def _mark_dead(self, exc: BaseException) -> None:
        """Mark the channel dead and fail every pending future.

        Idempotent. Calls after the first one update ``_reader_exc``
        only if it was None — the first cause wins.
        """
        if self._reader_exc is None:
            self._reader_exc = exc
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _stderr_loop(self) -> None:
        """Forward app-server stderr to our logger at DEBUG."""
        assert self._proc.stderr is not None
        try:
            async for raw in self._proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    logger.debug("codex app-server stderr: %s", line)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("codex app-server stderr reader crashed")

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route a parsed JSON-RPC message to its destination."""
        # Response (has id, has result or error)
        if "id" in msg and ("result" in msg or "error" in msg):
            request_id = msg["id"]
            future = self._pending.get(request_id)
            if future is None or future.done():
                # Late response or duplicate — log and drop. Not fatal.
                logger.debug("dropping response for unknown id %r", request_id)
                return
            if "error" in msg:
                err = msg["error"] or {}
                future.set_exception(
                    AppServerError(err.get("message", "unknown error"), err)
                )
            else:
                future.set_result(msg.get("result"))
            return

        # Notification (has method, no id)
        if "method" in msg and "id" not in msg:
            method = msg["method"]
            params = msg.get("params") or {}
            for cb in list(self._subscribers):
                try:
                    cb(method, params)
                except Exception:
                    logger.exception("notification subscriber raised on %r", method)
            return

        # Server-initiated request (has method, has id). Codex 0.130 uses
        # this for MCP-server elicitations: when a thread wants to call a
        # tool on a configured MCP server, the app-server sends
        # `mcpServer/elicitation/request` and waits up to 90s for the
        # client to answer with `{action: "accept"|"decline"|"cancel"}`.
        # Without a response, the turn wedges and times out at 600s with
        # `codex turn 019e... wedged: no events for 90s` — exactly the
        # post-PR#48 symptom observed on CR2 + Researcher 2026-05-24.
        #
        # For our agents the MCP server is the in-process `molecule`
        # adapter — we wrote it, we trust it, every call should be
        # auto-accepted. Future per-method policy can be plugged via
        # ``set_inbound_request_handler``.
        if "method" in msg and "id" in msg:
            method = msg["method"]
            request_id = msg["id"]
            handler = self._inbound_handlers.get(method)
            if handler is not None:
                # Caller-provided handler: schedule + send its result.
                asyncio.create_task(self._run_inbound_handler(handler, msg))
                return
            # Default behavior: auto-accept MCP elicitation requests;
            # log + decline anything else so the turn doesn't hang.
            if method == "mcpServer/elicitation/request":
                response = {"action": "accept", "content": {}}
            else:
                logger.warning(
                    "no handler for inbound request %r; auto-declining", method
                )
                response = {"action": "decline", "content": None}
            asyncio.create_task(
                self._write_message(
                    {"jsonrpc": "2.0", "id": request_id, "result": response}
                )
            )
            return

        logger.warning("unrecognized message from app-server: %r", msg)

    async def _run_inbound_handler(
        self, handler: "InboundRequestHandler", msg: dict[str, Any]
    ) -> None:
        """Invoke a caller-registered inbound-request handler + send response."""
        request_id = msg["id"]
        method = msg["method"]
        params = msg.get("params") or {}
        try:
            result = await handler(method, params)
            await self._write_message(
                {"jsonrpc": "2.0", "id": request_id, "result": result}
            )
        except Exception as exc:
            logger.exception("inbound request handler raised on %r", method)
            await self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
            )
