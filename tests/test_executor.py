"""Unit tests for CodexAppServerExecutor's internal turn lifecycle.

We don't stand up a real codex app-server — those tests live in
test_app_server.py which validates the JSON-RPC plumbing against a
mock binary. Here we focus on the protocol-level behavior of
``_run_turn``: thread bootstrap, notification accumulation, completion
detection, error surfacing, mid-turn serialization, and (the recent
addition) the no-deadlock guarantees when the channel goes wedged.

The fake AppServerProcess records every request sent and exposes a
helper to drive notifications + responses on demand. It deliberately
mirrors the JSON-RPC notification shape codex 0.72+ emits in
production (``method = "codex/event/<type>"`` with the payload under
``params.msg``), not the bare-method legacy form, so test failures
catch the same protocol bugs production hits.
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

from executor import (  # noqa: E402
    CodexAppServerExecutor,
    _TURN_INACTIVITY_TIMEOUT,
)
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
        # When set, request() raises this on the Nth call (1-indexed).
        # Lets a test simulate the channel going dead between turns.
        self.fail_request_n: int | None = None
        self.fail_request_exc: Exception | None = None
        self._request_count = 0

    async def initialize(self, *, client_info: dict) -> dict:
        return {"userAgent": "fake/0.0"}

    async def request(self, method: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
        self._request_count += 1
        self.requests.append((method, params or {}))
        if (
            self.fail_request_n is not None
            and self._request_count >= self.fail_request_n
            and self.fail_request_exc is not None
        ):
            raise self.fail_request_exc
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

    def subscribe(self, callback):
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
    def push_delta(self, text: str) -> None:
        """Push a streamed agent_message_delta (codex 0.72+ shape)."""
        self.push(
            "codex/event/agent_message_delta",
            {"msg": {"type": "agent_message_delta", "delta": text}},
        )

    def push_task_complete(self, last_message: str | None = None) -> None:
        """Push the canonical end-of-turn event (codex 0.72+ shape)."""
        msg: dict = {"type": "task_complete"}
        if last_message is not None:
            msg["last_agent_message"] = last_message
        self.push("codex/event/task_complete", {"msg": msg})

    def push_event_error(self, message: str) -> None:
        """Push a fatal error notification under the codex/event envelope."""
        self.push(
            "codex/event/error",
            {"msg": {"type": "error", "message": message}},
        )

    def push(self, method: str, params: dict | None = None) -> None:
        """Synchronously deliver a notification to all subscribers."""
        for cb in list(self._subscribers):
            cb(method, params or {})


def _make_executor(fake: FakeAppServer, *, model: str = "gpt-5.5", system_prompt: str = "be helpful") -> CodexAppServerExecutor:
    cfg = AdapterConfig(model=model, system_prompt=system_prompt)
    ex = CodexAppServerExecutor(cfg)
    # Pre-inject the fake so _ensure_thread skips spawning codex.
    ex._app_server = fake  # type: ignore[assignment]
    return ex


async def _wait_for_method(fake: FakeAppServer, method: str, *, after_count: int = 0) -> None:
    """Yield until ``method`` has been recorded at least ``after_count + 1`` times."""
    for _ in range(500):
        seen = sum(1 for m, _ in fake.requests if m == method)
        if seen > after_count:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(
        f"never saw {after_count + 1} call(s) to {method}; "
        f"requests so far: {[m for m, _ in fake.requests]}"
    )


@pytest.mark.asyncio
async def test_run_turn_starts_thread_and_returns_assembled_deltas() -> None:
    fake = FakeAppServer()
    ex = _make_executor(fake)

    async def driver() -> None:
        await _wait_for_method(fake, "turn/start")
        fake.push_delta("hello ")
        fake.push_delta("world")
        fake.push_task_complete()

    driver_task = asyncio.create_task(driver())
    text = await ex._run_turn("hi")
    await driver_task

    assert text == "hello world"
    methods = [m for m, _ in fake.requests]
    assert "thread/start" in methods
    assert "turn/start" in methods


@pytest.mark.asyncio
async def test_run_turn_reuses_thread_on_second_call() -> None:
    """The regression that wedged prod-Reviewer/Researcher 2026-05-18.

    Pre-fix the second ``_run_turn`` returned (FakeAppServer side it
    worked) but the real app-server's reader loop had exited on stdout
    EOF without failing pending requests — so the second
    ``state.completed.wait()`` would block until ``_TURN_TIMEOUT``.

    This unit exercises the executor's protocol contract (two turns
    reuse the thread, both assemble their deltas), and the live
    failure mode of the same multi-turn shape is covered in
    test_app_server.py::test_eof_fails_pending_requests.
    """
    fake = FakeAppServer()
    ex = _make_executor(fake)

    async def drive_one(text: str, turn_index: int) -> None:
        await _wait_for_method(fake, "turn/start", after_count=turn_index)
        fake.push_delta(text)
        fake.push_task_complete()

    t1 = asyncio.create_task(drive_one("first", 0))
    text1 = await ex._run_turn("ping")
    await t1

    t2 = asyncio.create_task(drive_one("second", 1))
    text2 = await ex._run_turn("pong")
    await t2

    assert text1 == "first"
    assert text2 == "second"
    # thread/start should fire EXACTLY once across both turns — turn 2
    # MUST reuse the thread, not re-bootstrap.
    thread_starts = sum(1 for m, _ in fake.requests if m == "thread/start")
    assert thread_starts == 1


@pytest.mark.asyncio
async def test_run_turn_accepts_codex_0130_completed_agent_message_item() -> None:
    """Codex 0.130 emits completed assistant items with camelCase types."""
    fake = FakeAppServer()
    ex = _make_executor(fake)

    async def driver() -> None:
        await _wait_for_method(fake, "turn/start")
        fake.push(
            "item/completed",
            {"item": {"type": "agentMessage", "message": "whole response"}},
        )
        fake.push("turn/completed")

    driver_task = asyncio.create_task(driver())
    text = await ex._run_turn("hi")
    await driver_task

    assert text == "whole response"


@pytest.mark.asyncio
async def test_run_turn_surfaces_error_notification() -> None:
    fake = FakeAppServer()
    ex = _make_executor(fake)

    async def driver() -> None:
        await _wait_for_method(fake, "turn/start")
        fake.push_event_error("model rate limited")

    driver_task = asyncio.create_task(driver())
    with pytest.raises(RuntimeError, match="rate limited"):
        await ex._run_turn("hi")
    await driver_task


@pytest.mark.asyncio
async def test_thread_start_passes_config() -> None:
    fake = FakeAppServer()
    ex = _make_executor(fake, model="o4-mini", system_prompt="custom prompt")

    async def driver() -> None:
        await _wait_for_method(fake, "turn/start")
        fake.push_task_complete()

    driver_task = asyncio.create_task(driver())
    await ex._run_turn("hi")
    await driver_task

    thread_start = next(p for m, p in fake.requests if m == "thread/start")
    assert thread_start["model"] == "o4-mini"
    assert thread_start["developerInstructions"] == "custom prompt"
    assert thread_start["approvalPolicy"] == "never"
    assert thread_start["sandboxPolicy"] == {"mode": "danger-full-access"}


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
            await _wait_for_method(fake, "turn/start", after_count=idx)
            starts.append(idx)
            fake.push_delta(f"r{idx}")
            fake.push_task_complete()
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


@pytest.mark.asyncio
async def test_inactivity_watchdog_surfaces_error_on_silent_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-05-18 wedge: codex stops emitting events mid-turn.

    Pre-fix, ``_run_turn`` would block on ``state.completed.wait()``
    for the full ``_TURN_TIMEOUT`` (10 minutes) when codex stopped
    sending events. Post-fix, the inactivity watchdog raises
    TimeoutError after ``_TURN_INACTIVITY_TIMEOUT`` seconds.

    We monkeypatch the watchdog timeout to a fraction of a second so
    the test runs in well under a second.
    """
    import executor as exec_mod

    monkeypatch.setattr(exec_mod, "_TURN_INACTIVITY_TIMEOUT", 0.3)

    fake = FakeAppServer()
    ex = _make_executor(fake)

    async def driver() -> None:
        await _wait_for_method(fake, "turn/start")
        # Emit ONE delta to confirm activity worked, then go silent.
        fake.push_delta("hello, ")
        # No task_complete, no further events — wedge.

    driver_task = asyncio.create_task(driver())
    with pytest.raises(asyncio.TimeoutError, match="channel wedged"):
        await ex._run_turn("hi")
    await driver_task

    # Lock must be released so the next caller doesn't inherit the wedge.
    assert not ex._turn_lock.locked()


@pytest.mark.asyncio
async def test_inactivity_watchdog_resets_on_each_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow-but-alive channel must NOT trip the watchdog.

    The inactivity watchdog fires only on gaps BETWEEN events. As long
    as codex keeps emitting (deltas, reasoning, tool I/O — anything
    that bumps ``state.activity``), the turn runs to its natural end
    even if total time exceeds _TURN_INACTIVITY_TIMEOUT.
    """
    import executor as exec_mod

    monkeypatch.setattr(exec_mod, "_TURN_INACTIVITY_TIMEOUT", 0.4)

    fake = FakeAppServer()
    ex = _make_executor(fake)

    async def driver() -> None:
        await _wait_for_method(fake, "turn/start")
        # Drip-feed events under the 0.4s inactivity bound.
        for chunk in ("a", "b", "c", "d", "e"):
            fake.push_delta(chunk)
            await asyncio.sleep(0.15)
        fake.push_task_complete()

    driver_task = asyncio.create_task(driver())
    text = await ex._run_turn("hi")
    await driver_task

    assert text == "abcde"


@pytest.mark.asyncio
async def test_second_turn_after_channel_dies_surfaces_error_promptly() -> None:
    """Second turn must NOT hang when the channel went dead after turn 1.

    Mirrors the 2026-05-18 prod-Reviewer/Researcher wedge: first turn
    completes, then the codex CLI's stdout closes (crash / EOF /
    silent). Pre-fix turn 2 hung on state.completed.wait() for 10
    minutes. Post-fix the executor surfaces the ConnectionError that
    bubbles from AppServerProcess.request().
    """
    fake = FakeAppServer()
    ex = _make_executor(fake)

    # Turn 1: succeeds cleanly.
    async def drive_one() -> None:
        await _wait_for_method(fake, "turn/start")
        fake.push_delta("ok")
        fake.push_task_complete()

    t1 = asyncio.create_task(drive_one())
    text1 = await ex._run_turn("hi 1")
    await t1
    assert text1 == "ok"

    # Turn 2: app-server now dead. Any new request() raises
    # ConnectionError (the same exception AppServerProcess raises when
    # _reader_exc is set by EOF detection).
    fake.fail_request_n = len(fake.requests) + 1
    fake.fail_request_exc = ConnectionError(
        "app-server stdout closed (EOF) — child exited or stopped writing"
    )

    with pytest.raises(ConnectionError, match="stdout closed"):
        await ex._run_turn("hi 2")

    # Lock must be released so the NEXT caller doesn't inherit the wedge.
    assert not ex._turn_lock.locked()


@pytest.mark.asyncio
async def test_thread_start_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """_ensure_thread() must NOT block indefinitely on a wedged child.

    Pre-fix a child wedged during initialize / thread-start would hang
    the executor's first turn for ``_DEFAULT_REQUEST_TIMEOUT`` (10 min).
    Post-fix we cap initialize and thread/start so the failure surfaces
    fast.

    The fake here enforces the ``timeout=`` kwarg the same way
    AppServerProcess.request does — wrapping the inner sleep in
    asyncio.wait_for — so the test exercises the real contract the
    executor relies on.
    """
    import executor as exec_mod

    # Drop the bootstrap timeouts to make the test run in well under a
    # second.
    monkeypatch.setattr(exec_mod, "_THREAD_START_TIMEOUT", 0.2)
    monkeypatch.setattr(exec_mod, "_INITIALIZE_TIMEOUT", 0.2)

    class WedgedAppServer(FakeAppServer):
        async def request(self, method: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
            self.requests.append((method, params or {}))
            if method == "thread/start":
                # Mimic AppServerProcess.request: enforce caller-passed
                # timeout via wait_for. A real wedged child would have
                # its request future never resolve; the wait_for is
                # what surfaces the TimeoutError.
                async def _never() -> dict:
                    await asyncio.sleep(10.0)
                    return {"thread": {"id": "never"}}
                return await asyncio.wait_for(
                    _never(),
                    timeout=timeout if timeout is not None else 10.0,
                )
            return await super().request(method, params, timeout=timeout)

    fake = WedgedAppServer()
    ex = _make_executor(fake)

    with pytest.raises(asyncio.TimeoutError):
        await ex._run_turn("hi")


# ----------------------------------------------------------------------
# Phase 1 file-only message support (a1ea2200 archaeology)
#
# chloe-dong canary 2026-05-20 01:04:27Z local time: PDF-only message
# returned opaque "(empty prompt — nothing to do)". The fix relaxes the
# empty-prompt guard so a file-only message synthesizes a prompt instead
# of short-circuiting, and the truly-empty case surfaces an actionable
# reason per feedback_surface_actionable_failure_reason_to_user.
# ----------------------------------------------------------------------


from types import SimpleNamespace
from unittest.mock import MagicMock


def _ctx_with_parts(parts: list) -> SimpleNamespace:
    """Build a minimal RequestContext stub the executor reads from.

    Only ``context.message.parts`` is touched by the guard path under
    test, so the surrounding object can stay light.
    """
    msg = SimpleNamespace(parts=parts, task_id=None, context_id=None)
    return SimpleNamespace(message=msg, task_id=None, session_id=None, context_id=None)


def _text_part(text: str) -> SimpleNamespace:
    return SimpleNamespace(kind="text", text=text)


def _file_part(*, name: str, mime_type: str, path: str) -> SimpleNamespace:
    """Build a v0-flat FilePart-shaped object.

    ``extract_attached_files`` calls ``resolve_attachment_uri`` which
    requires the uri to point at an existing file on disk — tests pass a
    real tmp_path.
    """
    file_obj = SimpleNamespace(uri=f"file://{path}", name=name, mimeType=mime_type)
    return SimpleNamespace(kind="file", file=file_obj)


class _CapturingQueue:
    def __init__(self) -> None:
        self.events: list = []

    async def enqueue_event(self, event) -> None:  # type: ignore[no-untyped-def]
        self.events.append(event)


@pytest.mark.asyncio
async def test_execute_file_only_no_longer_returns_opaque_empty(
    tmp_path, monkeypatch
) -> None:
    """File-only message must not short-circuit with the opaque
    '(empty prompt — nothing to do)' string."""
    # extract_attached_files / resolve_attachment_uri refuse paths
    # outside WORKSPACE_MOUNT. Point that at the test's tmp_path so the
    # helper accepts our fixture file.
    import molecule_runtime.executor_helpers as _helpers
    monkeypatch.setattr(_helpers, "WORKSPACE_MOUNT", str(tmp_path))

    fake = FakeAppServer()
    ex = _make_executor(fake)

    pdf = tmp_path / "chloe.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub\n")

    ctx = _ctx_with_parts([
        _file_part(name="chloe.pdf", mime_type="application/pdf", path=str(pdf)),
    ])
    queue = _CapturingQueue()

    captured_prompts: list[str] = []

    async def fake_run_turn(prompt: str) -> str:
        captured_prompts.append(prompt)
        return "ack"

    ex._run_turn = fake_run_turn  # type: ignore[assignment,method-assign]
    await ex.execute(ctx, queue)

    # Either the synthesized prompt landed in _run_turn OR the reply
    # event went out — never the opaque empty-prompt string.
    blob = repr(queue.events) + repr(captured_prompts)
    assert "empty prompt — nothing to do" not in blob
    # Prompt must mention the file name so codex can act on it.
    assert any("chloe.pdf" in p for p in captured_prompts)


@pytest.mark.asyncio
async def test_execute_image_attachment_becomes_local_image_input(
    tmp_path, monkeypatch
) -> None:
    """PNG file parts must reach codex app-server as localImage items."""
    import molecule_runtime.executor_helpers as _helpers
    monkeypatch.setattr(_helpers, "WORKSPACE_MOUNT", str(tmp_path))

    fake = FakeAppServer()
    ex = _make_executor(fake)

    png = tmp_path / "shape-probe.png"
    png.write_bytes(b"png")
    ctx = _ctx_with_parts([
        _text_part("describe the image"),
        _file_part(name="shape-probe.png", mime_type="image/png", path=str(png)),
    ])
    queue = _CapturingQueue()

    async def driver() -> None:
        await _wait_for_method(fake, "turn/start")
        fake.push_delta("red square")
        fake.push_task_complete()

    driver_task = asyncio.create_task(driver())
    await ex.execute(ctx, queue)
    await driver_task

    turn_start = [p for m, p in fake.requests if m == "turn/start"][-1]
    assert turn_start["input"][0]["type"] == "text"
    assert "shape-probe.png" in turn_start["input"][0]["text"]
    assert {"type": "localImage", "path": str(png)} in turn_start["input"]


@pytest.mark.asyncio
async def test_execute_truly_empty_surfaces_actionable_reason() -> None:
    """Empty text AND no files → actionable user-facing reason, NOT
    opaque error type."""
    fake = FakeAppServer()
    ex = _make_executor(fake)

    ctx = _ctx_with_parts([_text_part("   ")])
    queue = _CapturingQueue()

    await ex.execute(ctx, queue)

    assert len(queue.events) == 1
    msg_repr = repr(queue.events[0])
    assert "Your message was empty" in msg_repr
    assert "send text or a file" in msg_repr
    # The old opaque string must NOT appear.
    assert "(empty prompt — nothing to do)" not in msg_repr


@pytest.mark.asyncio
async def test_execute_text_only_still_passes_prompt_unchanged() -> None:
    """Regression-pin: text-only messages keep working exactly as
    before — the file-aware branch must not perturb the text path."""
    fake = FakeAppServer()
    ex = _make_executor(fake)

    ctx = _ctx_with_parts([_text_part("write a haiku")])
    queue = _CapturingQueue()

    captured_prompts: list[str] = []

    async def fake_run_turn(prompt: str) -> str:
        captured_prompts.append(prompt)
        return "ok"

    ex._run_turn = fake_run_turn  # type: ignore[assignment,method-assign]
    await ex.execute(ctx, queue)

    assert captured_prompts == ["write a haiku"]
