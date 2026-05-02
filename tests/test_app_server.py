"""Unit tests for AppServerProcess against a fake stdio child.

We can't depend on a real `codex` binary in CI, so these tests stand up
a Python-implemented mock app-server that speaks NDJSON over stdio.
The mock is intentionally tiny — it only handles the request/response
+ notification semantics we exercise here, not the full v2 protocol.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

# Make app_server.py importable from the test file without setup.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app_server import AppServerError, AppServerProcess  # noqa: E402

# Path to the in-tree mock app-server (a Python script that pretends
# to be `codex app-server`). Tests pass it via executable= override.
_MOCK = str(Path(__file__).resolve().parent / "mock_app_server.py")


@pytest.mark.asyncio
async def test_initialize_handshake() -> None:
    proc = await AppServerProcess.start(executable=sys.executable, args=(_MOCK,))
    try:
        result = await proc.initialize(client_info={"name": "test", "version": "0"})
        assert result["userAgent"].startswith("mock_app_server/")
    finally:
        await proc.close()


@pytest.mark.asyncio
async def test_request_response_correlation() -> None:
    """Concurrent requests should not cross responses."""
    proc = await AppServerProcess.start(executable=sys.executable, args=(_MOCK,))
    try:
        await proc.initialize(client_info={"name": "test", "version": "0"})

        # Mock supports `echo` which round-trips params.text after a
        # configurable delay. Send three with different delays + texts;
        # confirm each future resolves to its own input.
        results = await asyncio.gather(
            proc.request("echo", {"text": "alpha", "delay_ms": 30}),
            proc.request("echo", {"text": "beta", "delay_ms": 5}),
            proc.request("echo", {"text": "gamma", "delay_ms": 15}),
        )
        assert [r["text"] for r in results] == ["alpha", "beta", "gamma"]
    finally:
        await proc.close()


@pytest.mark.asyncio
async def test_error_response_raises_app_server_error() -> None:
    proc = await AppServerProcess.start(executable=sys.executable, args=(_MOCK,))
    try:
        await proc.initialize(client_info={"name": "test", "version": "0"})
        with pytest.raises(AppServerError) as ei:
            await proc.request("error", {"code": -32000, "message": "boom"})
        assert "boom" in str(ei.value)
        assert ei.value.payload.get("code") == -32000
    finally:
        await proc.close()


@pytest.mark.asyncio
async def test_notifications_dispatched_to_subscribers() -> None:
    """Subscribed callback should fire for every notification, in order."""
    proc = await AppServerProcess.start(executable=sys.executable, args=(_MOCK,))
    received: list[tuple[str, dict]] = []
    try:
        await proc.initialize(client_info={"name": "test", "version": "0"})
        proc.subscribe(lambda m, p: received.append((m, p)))
        # Mock's `emit` request fires N notifications named `tick` then
        # returns a final ack. We need to wait until all notifications
        # arrive — the mock guarantees the response is sent AFTER its
        # notifications, so awaiting the response is sufficient.
        await proc.request("emit", {"count": 3, "method": "tick"})
        # Give the reader loop one tick to process trailing notifications
        # if any (defensive — mock orders them before the response).
        await asyncio.sleep(0.05)
        assert [m for m, _ in received] == ["tick", "tick", "tick"]
        assert [p["i"] for _, p in received] == [0, 1, 2]
    finally:
        await proc.close()


@pytest.mark.asyncio
async def test_pending_requests_fail_on_close() -> None:
    """close() must release any awaiting request callers."""
    proc = await AppServerProcess.start(executable=sys.executable, args=(_MOCK,))
    try:
        await proc.initialize(client_info={"name": "test", "version": "0"})
        # Fire a long-delay request and close the process before the
        # response can arrive. The pending future should fail with
        # ConnectionError so the caller doesn't hang.
        slow = asyncio.create_task(proc.request("echo", {"text": "x", "delay_ms": 5000}))
        await asyncio.sleep(0.05)
        await proc.close()
        with pytest.raises(ConnectionError):
            await slow
    finally:
        # Idempotent
        await proc.close()


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    proc = await AppServerProcess.start(executable=sys.executable, args=(_MOCK,))
    await proc.close()
    rc = await proc.close()
    assert rc is not None  # mock exits with 0


@pytest.mark.asyncio
async def test_request_after_close_raises() -> None:
    proc = await AppServerProcess.start(executable=sys.executable, args=(_MOCK,))
    await proc.close()
    with pytest.raises(ConnectionError):
        await proc.request("echo", {"text": "x"})
