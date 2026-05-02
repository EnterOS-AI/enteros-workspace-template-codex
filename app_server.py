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
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
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
        proc = await asyncio.create_subprocess_exec(
            executable,
            *args,
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
        return instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def initialize(self, *, client_info: dict[str, Any]) -> dict[str, Any]:
        """Send the `initialize` handshake. Must be called before other RPCs."""
        return await self.request("initialize", {"clientInfo": client_info})

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
        for task in (self._reader_task, self._stderr_task):
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
        """Drain stdout line-by-line, route messages by id."""
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
            self._reader_exc = exc
            # Fail all pending requests so callers don't hang on a dead pipe.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(exc)
            self._pending.clear()
            raise

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

        logger.warning("unrecognized message from app-server: %r", msg)
