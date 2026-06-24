"""Inventory-side loaded_mcp_tools tests for codex (#142 / #3082).

These tests pin the producer fix: instead of only reporting MCP tools the
model *invoked* during the current turn, the codex executor enumerates the
actually-loaded tool declarations from each server configured in
``~/.codex/config.toml`` and reports them as ``mcp__<server>__<tool>``.

A healthy codex concierge with the molecule-platform management MCP loaded
therefore reports ``mcp__molecule-platform__create_workspace`` even on turns
where the model did not call it, preventing the platform online/degraded gate
from fail-closing a working concierge.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("a2a.helpers")
pytest.importorskip("molecule_runtime.adapters.base")

from executor import (  # noqa: E402
    CodexAppServerExecutor,
    _read_codex_mcp_servers,
    extract_loaded_mcp_tools,
)
from molecule_runtime.adapters.base import AdapterConfig  # noqa: E402


_FAKE_MCP_SERVER = '''
import json
import sys

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = req.get("method", "")
    req_id = req.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "fake-mcp", "version": "0.0"},
        }})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "tools": [
                {"name": "create_workspace"},
                {"name": "list_workspaces"},
                {"name": "delegate_task"},
            ]
        }})
'''


def _write_fake_server(tmp_path: Path) -> Path:
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(_FAKE_MCP_SERVER)
    script.chmod(0o755)
    return script


def test_read_codex_mcp_servers_missing_config(tmp_path: Path) -> None:
    """No config file → no servers → empty inventory."""
    assert _read_codex_mcp_servers(tmp_path / "nonexistent" / "config.toml") == {}


def test_read_codex_mcp_servers_parses_toml(tmp_path: Path, monkeypatch) -> None:
    """The helper reads server name/spec from [mcp_servers]."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    config = home / ".codex" / "config.toml"
    config.write_text(
        '[mcp_servers.molecule-platform]\n'
        'command = "molecule-mcp"\n'
        '[mcp_servers.molecule-platform.env]\n'
        'MOLECULE_MCP_MODE = "management"\n'
    )
    servers = _read_codex_mcp_servers()
    assert "molecule-platform" in servers
    assert servers["molecule-platform"]["command"] == "molecule-mcp"
    assert servers["molecule-platform"]["env"]["MOLECULE_MCP_MODE"] == "management"


@pytest.mark.asyncio
async def test_extract_loaded_mcp_tools_empty_when_no_servers(
    tmp_path: Path, monkeypatch
) -> None:
    """No MCP servers configured → inventory stays empty (degraded)."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    (home / ".codex" / "config.toml").write_text("# empty config\n")

    assert await extract_loaded_mcp_tools() == []


@pytest.mark.asyncio
async def test_extract_loaded_mcp_tools_enumerates_fake_server(
    tmp_path: Path, monkeypatch
) -> None:
    """A configured server is enumerated and IDs normalized."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    fake = _write_fake_server(tmp_path)
    config = home / ".codex" / "config.toml"
    config.write_text(
        f'[mcp_servers.molecule-platform]\n'
        f'command = "{sys.executable}"\n'
        f'args = ["{fake}"]\n'
    )

    tools = await extract_loaded_mcp_tools()
    assert "mcp__molecule-platform__create_workspace" in tools
    assert "mcp__molecule-platform__list_workspaces" in tools
    assert "mcp__molecule-platform__delegate_task" in tools
    assert tools == sorted(tools)


@pytest.mark.asyncio
async def test_extract_loaded_mcp_tools_ignores_broken_server(
    tmp_path: Path, monkeypatch
) -> None:
    """A server that fails handshake is ignored; others still enumerate."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    fake = _write_fake_server(tmp_path)
    config = home / ".codex" / "config.toml"
    config.write_text(
        '[mcp_servers.broken]\n'
        'command = "false"\n'
        f'[mcp_servers.molecule-platform]\n'
        f'command = "{sys.executable}"\n'
        f'args = ["{fake}"]\n'
    )

    tools = await extract_loaded_mcp_tools()
    assert "mcp__molecule-platform__create_workspace" in tools
    assert all(not t.startswith("mcp__broken__") for t in tools)


@pytest.mark.asyncio
async def test_executor_reports_loaded_inventory(
    tmp_path: Path, monkeypatch
) -> None:
    """A turn reports create_workspace via set_loaded_mcp_tools when the
    toolset is loaded, even though the fake turn never invoked it."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    fake = _write_fake_server(tmp_path)
    config = home / ".codex" / "config.toml"
    config.write_text(
        f'[mcp_servers.molecule-platform]\n'
        f'command = "{sys.executable}"\n'
        f'args = ["{fake}"]\n'
    )

    # Spy on the set_loaded_mcp_tools callable used by executor.py.
    import executor as executor_mod

    recorded: list[list[str]] = []
    original = executor_mod.set_loaded_mcp_tools
    executor_mod.set_loaded_mcp_tools = lambda tools: recorded.append(list(tools))

    ex = CodexAppServerExecutor(AdapterConfig(model="gpt-5.5"))
    # Patch _ensure_thread so we don't need a real codex app-server.
    fake_server = _FakeAppServer()
    ex._app_server = fake_server  # type: ignore[assignment]
    ex._thread_id = "th_1"

    async def driver() -> None:
        # Wait for turn/start then complete the turn.
        for _ in range(500):
            if any(m == "turn/start" for m, _ in fake_server.requests):
                fake_server.push_delta("hello")
                fake_server.push_task_complete("hello")
                return
            await asyncio.sleep(0.005)

    task = asyncio.create_task(driver())
    text = await ex._run_turn("hi")
    await task
    assert text == "hello"

    assert len(recorded) == 1
    assert "mcp__molecule-platform__create_workspace" in recorded[0]

    # Restore original to avoid leaking the spy to other tests.
    executor_mod.set_loaded_mcp_tools = original


@pytest.mark.asyncio
async def test_executor_invoked_tool_auxiliary_when_enumeration_empty(
    tmp_path: Path, monkeypatch
) -> None:
    """If inventory enumeration yields nothing, a tool the model DID invoke
    this turn is still reported as auxiliary proof the server is live."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    # Configured server that fails handshake (no tools enumerated).
    config = home / ".codex" / "config.toml"
    config.write_text('[mcp_servers.broken]\ncommand = "false"\n')

    import executor as executor_mod

    recorded: list[list[str]] = []
    original = executor_mod.set_loaded_mcp_tools
    executor_mod.set_loaded_mcp_tools = lambda tools: recorded.append(list(tools))

    ex = CodexAppServerExecutor(AdapterConfig(model="gpt-5.5"))
    fake_server = _FakeAppServer()
    ex._app_server = fake_server  # type: ignore[assignment]
    ex._thread_id = "th_1"

    async def driver() -> None:
        for _ in range(500):
            if any(m == "turn/start" for m, _ in fake_server.requests):
                # Emit an MCP tool-call item, then complete.
                fake_server.push(
                    "item/completed",
                    {
                        "item": {
                            "type": "mcp_tool_call",
                            "server": "molecule-platform",
                            "tool": "create_workspace",
                        }
                    },
                )
                fake_server.push_task_complete("done")
                return
            await asyncio.sleep(0.005)

    task = asyncio.create_task(driver())
    text = await ex._run_turn("hi")
    await task
    assert text == "done"

    assert len(recorded) == 1
    assert "mcp__molecule-platform__create_workspace" in recorded[0]

    executor_mod.set_loaded_mcp_tools = original


class _FakeAppServer:
    """Minimal AppServerProcess stand-in for the executor turn test."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self._subscribers: list = []

    async def initialize(self, *, client_info: dict) -> dict:
        return {"userAgent": "fake/0.0"}

    async def request(self, method: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
        self.requests.append((method, params or {}))
        if method == "turn/start":
            return {"turn": {"id": "tu_1"}}
        raise AssertionError(f"unexpected method: {method}")

    def subscribe(self, callback):
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def push_delta(self, text: str) -> None:
        self.push(
            "codex/event/agent_message_delta",
            {"msg": {"type": "agent_message_delta", "delta": text}},
        )

    def push_task_complete(self, last_message: str | None = None) -> None:
        msg: dict = {"type": "task_complete"}
        if last_message is not None:
            msg["last_agent_message"] = last_message
        self.push("codex/event/task_complete", {"msg": msg})

    def push(self, method: str, params: dict | None = None) -> None:
        for cb in list(self._subscribers):
            cb(method, params or {})
