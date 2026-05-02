"""Unit tests for CodexAppServerExecutor's internal turn lifecycle.

We don't stand up a real codex app-server — those tests live in
test_app_server.py which validates the JSON-RPC plumbing against a
mock binary. Here we focus on the protocol-level behavior of
``_run_turn``: thread bootstrap, notification accumulation, completion
detection, error surfacing, mid-turn serialization.

The fake AppServerProcess records every request sent and exposes a
helper to drive notifications + responses on demand.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Import the executor module — relies on a2a + molecule_runtime being
# installed locally. If not, skip these tests; the executor will still
# be exercised in container CI.
pytest.importorskip("a2a.helpers")
pytest.importorskip("molecule_runtime.adapters.base")

from executor import CodexAppServerExecutor  # noqa: E402
from molecule_runtime.adapters.base import AdapterConfig  # noqa: E402


class FakeAppServer:
    """Drop-in for AppServerProcess that lets tests script responses + notifications.

    Honors only the shape AppServerProcess presents to the executor:
    initialize / request / subscribe / close. Each scripted turn lets
    the test push delta notifications and resolve the response.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self._subscribers: list = []
        self._next_thread = 0
        self._next_turn = 0
        self.closed = False
        # Test-controllable knobs
        self.thread_start_response: dict | None = None
        self.turn_start_responses: list[dict] = []
        self.turn_start_raises: Exception | None = None

    async def initialize(self, *, client_info: dict) -> dict:
        return {"userAgent": "fake/0.0"}

    async def request(self, method: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
        self.requests.append((method, params or {}))
        if method == "thread/start":
            if self.thread_start_response is not None:
                return self.thread_start_response
            self._next_thread += 1
            # Use the real binary's `id` shape (verified 2026-05-02
            # against codex 0.72) — the schema's `threadId` is also
            # accepted by the executor but `id` is what production hits.
            return {"thread": {"id": f"th_{self._next_thread}"}}
        if method == "turn/start":
            if self.turn_start_raises:
                raise self.turn_start_raises
            if self.turn_start_responses:
                return self.turn_start_responses.pop(0)
            self._next_turn += 1
            return {"turn": {"id": f"tu_{self._next_turn}"}}
        if method == "turn/interrupt":
            return {}
        raise AssertionError(f"unexpected method: {method}")

    def subscribe(self, callback) -> "asyncio.coroutines.Coroutine":
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    async def close(self) -> int | None:
        self.closed = True
        return 0

    # --- test helpers ---------------------------------------------------
    def push(self, method: str, params: dict | None = None) -> None:
        """Synchronously deliver a notification to all subscribers."""
        for cb in list(self._subscribers):
            cb(method, params or {})


def _make_executor(fake: FakeAppServer, *, model: str = "gpt-5", system_prompt: str = "be helpful") -> CodexAppServerExecutor:
    cfg = AdapterConfig(model=model, system_prompt=system_prompt)
    ex = CodexAppServerExecutor(cfg)
    # Pre-inject the fake so _ensure_thread skips spawning codex.
    ex._app_server = fake  # type: ignore[assignment]
    return ex


@pytest.mark.asyncio
async def test_run_turn_starts_thread_and_returns_assembled_deltas() -> None:
    fake = FakeAppServer()
    ex = _make_executor(fake)

    async def driver() -> None:
        # Wait until turn/start is recorded, then push deltas + completion.
        for _ in range(50):
            if any(m == "turn/start" for m, _ in fake.requests):
                break
            await asyncio.sleep(0.01)
        fake.push("agent_message_delta", {"delta": "hello "})
        fake.push("agent_message_delta", {"delta": "world"})
        fake.push("turn/completed", {"turnId": "tu_1"})

    driver_task = asyncio.create_task(driver())
    text = await ex._run_turn("hi")
    await driver_task

    assert text == "hello world"
    methods = [m for m, _ in fake.requests]
    assert "thread/start" in methods
    assert "turn/start" in methods


@pytest.mark.asyncio
async def test_run_turn_reuses_thread_on_second_call() -> None:
    fake = FakeAppServer()
    ex = _make_executor(fake)

    async def drive_one(text: str, turn_id: str) -> None:
        for _ in range(50):
            count = sum(1 for m, _ in fake.requests if m == "turn/start")
            if count >= int(turn_id.split("_")[1]):
                break
            await asyncio.sleep(0.01)
        fake.push("agent_message_delta", {"delta": text})
        fake.push("turn/completed", {"turnId": turn_id})

    t1 = asyncio.create_task(drive_one("first", "tu_1"))
    text1 = await ex._run_turn("ping")
    await t1

    t2 = asyncio.create_task(drive_one("second", "tu_2"))
    text2 = await ex._run_turn("pong")
    await t2

    assert text1 == "first"
    assert text2 == "second"
    # thread/start should fire EXACTLY once across both turns.
    thread_starts = sum(1 for m, _ in fake.requests if m == "thread/start")
    assert thread_starts == 1


@pytest.mark.asyncio
async def test_run_turn_surfaces_error_notification() -> None:
    fake = FakeAppServer()
    ex = _make_executor(fake)

    async def driver() -> None:
        for _ in range(50):
            if any(m == "turn/start" for m, _ in fake.requests):
                break
            await asyncio.sleep(0.01)
        fake.push("error_notification", {"message": "model rate limited"})

    driver_task = asyncio.create_task(driver())
    with pytest.raises(RuntimeError, match="rate limited"):
        await ex._run_turn("hi")
    await driver_task


@pytest.mark.asyncio
async def test_thread_start_passes_config() -> None:
    fake = FakeAppServer()
    ex = _make_executor(fake, model="o4-mini", system_prompt="custom prompt")

    async def driver() -> None:
        for _ in range(50):
            if any(m == "turn/start" for m, _ in fake.requests):
                break
            await asyncio.sleep(0.01)
        fake.push("turn/completed", {"turnId": "tu_1"})

    driver_task = asyncio.create_task(driver())
    await ex._run_turn("hi")
    await driver_task

    thread_start = next(p for m, p in fake.requests if m == "thread/start")
    assert thread_start["model"] == "o4-mini"
    assert thread_start["developerInstructions"] == "custom prompt"
    assert thread_start["approvalPolicy"] == "never"
    assert thread_start["sandboxPolicy"] == {"mode": "workspace-write"}


@pytest.mark.asyncio
async def test_turn_lock_serializes_concurrent_executes() -> None:
    """Two concurrent execute()s should run their turns one-at-a-time."""
    fake = FakeAppServer()
    ex = _make_executor(fake)

    # Track the order in which turns START vs COMPLETE.
    starts: list[int] = []
    completes: list[int] = []

    async def execute_turn(idx: int, prompt: str) -> str:
        # Drive completion AFTER seeing this turn's turn/start in the
        # request log. Because of the lock, turn idx 2 won't start
        # until turn idx 1 is acked.
        async def driver() -> None:
            target_count = idx + 1
            for _ in range(200):
                count = sum(1 for m, _ in fake.requests if m == "turn/start")
                if count >= target_count:
                    starts.append(idx)
                    break
                await asyncio.sleep(0.005)
            fake.push("agent_message_delta", {"delta": f"r{idx}"})
            fake.push("turn/completed", {"turnId": f"tu_{idx + 1}"})
            completes.append(idx)

        driver_task = asyncio.create_task(driver())

        # Mirror the lock-and-run path execute() uses, without needing
        # an EventQueue.
        async with ex._turn_lock:
            text = await ex._run_turn(prompt)
        await driver_task
        return text

    results = await asyncio.gather(execute_turn(0, "first"), execute_turn(1, "second"))

    assert results == ["r0", "r1"] or results == ["r1", "r0"]
    # Whichever order tasks acquired the lock, the LOCK guarantees
    # turn N+1 doesn't start until turn N has completed. So starts and
    # completes should interleave one-at-a-time, not overlap.
    assert sorted(starts) == [0, 1]
    assert sorted(completes) == [0, 1]
    # Strict ordering check: between any two `starts` events, there
    # must be a corresponding `completes` event.
    assert starts[0] in completes[:1]
