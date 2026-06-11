"""Tiny mock of `codex app-server` for unit tests.

Speaks NDJSON over stdio. Implements only the methods AppServerProcess
tests exercise: `initialize`, `echo`, `error`, `emit`. Everything else
returns a JSON-RPC method-not-found error.

This stands in for the real codex binary so tests don't depend on a
specific codex-cli version installed on the runner. Keep it dumb — any
behavior the executor relies on must be tested against the real
binary in an integration test, not here.

Two failure modes are exposed for testing the reader-lifecycle
hardening (added 2026-05-18 alongside the prod-Reviewer/Researcher
wedge fix):

- ``close_stdout_after`` request: the mock answers normally, then
  closes its stdout file descriptor without exiting. This reproduces
  the codex CLI behavior of closing the channel mid-conversation
  while the process itself stays alive — the case the reader-EOF
  detection path needs to catch.

- ``crash_after`` request: the mock answers normally, then calls
  ``os._exit(1)`` shortly after. Reproduces a child that segfaults /
  is OOM-killed mid-turn — the case the child-watcher path catches.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys


async def _read_lines() -> "asyncio.StreamReader":
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    return reader


def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


# Server-side log of notifications the client sent us, in arrival order.
# Tests can read this back via the `get_received_notifications` RPC to
# assert that the client honored protocol contracts like sending an
# `initialized` notification after `initialize`.
_received_notifications: list[str] = []

# Pending server-initiated requests: maps request_id -> future that the
# read loop resolves with the client's response payload. Used by the
# `send_inbound_request` test helper to round-trip a server→client→server
# request/response pair under test.
_pending_inbound: dict = {}


async def _handle(msg: dict) -> None:
    method = msg.get("method")
    params = msg.get("params") or {}
    request_id = msg.get("id")

    # Notifications are recorded synchronously in main() before dispatch
    # (see the read loop below) so ordering vs. subsequent request handlers
    # is guaranteed by the read loop itself, not by task-FIFO scheduling.
    if request_id is None:
        return

    if method == "get_received_notifications":
        # Test-only introspection RPC: returns the list of notification
        # methods we've seen so far. Not part of the real codex protocol.
        _write({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"methods": list(_received_notifications)},
        })
        return

    if method == "initialize":
        _write({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"userAgent": "mock_app_server/0.1"},
        })
        return

    if method == "echo":
        delay_ms = int(params.get("delay_ms", 0))
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000)
        _write({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"text": params.get("text", "")},
        })
        return

    if method == "error":
        _write({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": int(params.get("code", -32000)),
                "message": str(params.get("message", "mock error")),
            },
        })
        return

    if method == "send_inbound_request":
        # Server-initiated request — fire it at the client + wait for a
        # response on a NEW request_id we control. Used to test
        # set_inbound_request_handler / default policy / elicitation
        # auto-accept behavior in app_server.AppServerProcess._dispatch.
        #
        # params: {req_method, req_id, req_params}
        req_method = str(params.get("req_method", "test/echo"))
        req_id = int(params.get("req_id", 99999))
        req_params = params.get("req_params") or {}
        # Register an event so the test can find the client's response.
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        _pending_inbound[req_id] = fut
        _write({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": req_method,
            "params": req_params,
        })
        # Wait briefly for the client to respond; relay the response in our
        # OWN reply so the test can assert on it.
        try:
            client_response = await asyncio.wait_for(fut, timeout=2.0)
        except asyncio.TimeoutError:
            client_response = {"timeout": True}
        finally:
            _pending_inbound.pop(req_id, None)
        _write({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"client_response": client_response},
        })
        return

    if method == "emit":
        # Fire `count` notifications named `method`, then ack.
        count = int(params.get("count", 0))
        notif_method = str(params.get("method", "tick"))
        for i in range(count):
            _write({
                "jsonrpc": "2.0",
                "method": notif_method,
                "params": {"i": i},
            })
        _write({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"emitted": count},
        })
        return

    if method == "close_stdout_after":
        # Ack the request, then close stdout without exiting. The
        # reader sees EOF; the child stays alive (so wait4 does NOT
        # return). Exercises the EOF path of _read_loop.
        #
        # We close the underlying file descriptor (FD 1), not just the
        # Python wrapper — closing sys.stdout only closes the Python
        # buffer; the OS pipe needs ``os.close(1)`` to actually send
        # EOF to the parent's reader.
        _write({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"closed": True},
        })
        sys.stdout.flush()
        try:
            os.close(1)
        except Exception:
            pass
        # Keep stdin draining so the process doesn't crash on next
        # read — we want EOF on stdout WITHOUT the watcher firing.
        try:
            while True:
                await asyncio.sleep(60)
        except Exception:
            return
        return

    if method == "crash_after":
        # Ack, then exit non-zero a moment later. Exercises the
        # _watch_child path: pending requests must fail when the
        # child reaps, regardless of whether the reader noticed
        # stdout EOF first.
        _write({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"crashing": True},
        })
        sys.stdout.flush()
        await asyncio.sleep(0.05)
        os._exit(1)

    # Method not found.
    _write({
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    })


async def main() -> None:
    reader = await _read_lines()
    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            msg = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        # Record notifications synchronously here so a subsequent request
        # observing _received_notifications cannot race the notification's
        # handler task. The handler still runs (no-op for notifications)
        # but the visible-state mutation is ordered by the read loop, not
        # by task FIFO. Without this, the test_initialize_sends_initialized_
        # notification assertion would flake the moment anyone added an
        # `await` before the append in _handle.
        if msg.get("id") is None and isinstance(msg.get("method"), str):
            _received_notifications.append(msg["method"])
        # Client response to a server-initiated request we sent earlier?
        # (response has id + (result or error) and no method.) Route to
        # the waiting future so send_inbound_request can return it.
        if (
            "id" in msg
            and "method" not in msg
            and ("result" in msg or "error" in msg)
        ):
            fut = _pending_inbound.get(msg["id"])
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_result({"error": msg["error"]})
                else:
                    fut.set_result(msg.get("result"))
                continue
        # Schedule handling so `emit` doesn't block subsequent reads.
        asyncio.create_task(_handle(msg))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
