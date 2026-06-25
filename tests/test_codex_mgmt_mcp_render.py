"""Render-matrix + producer tests for the codex management-MCP wiring (P2).

These tests pin the three load-bearing halves of "the org-admin management MCP
loads on a CODEX concierge":

  1. The codex adapter's ``setup()`` drives the base plugin pipeline so a
     DECLARED ``molecule-platform-mcp`` plugin is rendered into the file the
     codex CLI actually reads — ``~/.codex/config.toml`` ``[mcp_servers.*]`` —
     and NOT ``.claude/settings.json`` (the #3159 class of bug, here: not
     wired at all because the pipeline was never called).

  2. The codex ``register_mcp_server_hook`` override injects the LITERAL
     molecule-* env (MOLECULE_CP_URL / MOLECULE_ADMIN_TOKEN / PLATFORM_URL …)
     into the rendered ``[mcp_servers.molecule-platform.env]`` block — because
     codex's MCP-child env whitelist drops molecule-* vars, so without literals
     the management MCP can't reach the controlplane.

  3. The executor's loaded_mcp_tools producer (codex#3082) maps a codex
     MCP-tool-call item to a ``mcp__<server>__<tool>`` id.

The prove-fail: revert the ``install_plugins_via_registry`` call in
``adapter.setup()`` and ``test_setup_renders_platform_mcp_into_codex_config``
fails because ``[mcp_servers.molecule-platform]`` is never written.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

import pytest


def _make_platform_mcp_plugin(plugins_dir: Path) -> Path:
    """Create a minimal molecule-platform-mcp plugin dir (mcp-servers.json).

    The descriptor mirrors the real plugin: a runtime-agnostic
    ``name -> {command, args, env}`` carrying only MOLECULE_MCP_MODE — the
    controlplane-reach env (CP_URL/ADMIN_TOKEN) is whitelist-dropped by codex
    and must be filled by the adapter override from the process env.
    """
    plugin = plugins_dir / "molecule-platform-mcp"
    plugin.mkdir(parents=True)
    (plugin / "mcp-servers.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "molecule-platform": {
                        "command": "npx",
                        "args": ["-y", "@molecule-ai/mcp-server"],
                        "env": {"MOLECULE_MCP_MODE": "management"},
                    }
                }
            }
        )
    )
    return plugin


@pytest.mark.asyncio
async def test_setup_renders_platform_mcp_into_codex_config(
    monkeypatch, tmp_path
):
    """A config declaring the plugin → ``[mcp_servers.molecule-platform]`` is
    written into ~/.codex/config.toml, with literal molecule-* env."""
    if not shutil.which("codex"):
        pytest.skip("codex binary not on PATH (container-only check)")

    # Isolate HOME so mcp_render._codex_path writes into the tmp tree, and
    # CODEX_HOME so the adapter's config.toml render uses the same place.
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))

    # Satisfy the credential preflight (Mode A: OPENAI_API_KEY).
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("MOLECULE_LLM_BILLING_MODE", raising=False)

    # The whitelist-dropped runtime env the override must inject as literals.
    monkeypatch.setenv("MOLECULE_CP_URL", "https://cp.example.test")
    monkeypatch.setenv("MOLECULE_ADMIN_TOKEN", "admin-tok-xyz")
    monkeypatch.setenv("PLATFORM_URL", "http://platform:8080")
    monkeypatch.setenv("WORKSPACE_ID", "ws-codex-concierge")

    # Declared-plugin source dir the adapter loads from.
    configs = tmp_path / "configs"
    plugins_dir = configs / "plugins"
    _make_platform_mcp_plugin(plugins_dir)
    monkeypatch.setenv("PLUGINS_DIR", str(tmp_path / "no-shared-plugins"))

    from adapter import CodexAdapter
    from molecule_runtime.adapters.base import AdapterConfig

    cfg = AdapterConfig(
        model="gpt-5.5",
        config_path=str(configs),
        workspace_id="ws-codex-concierge",
    )
    await CodexAdapter().setup(cfg)

    config_toml = home / ".codex" / "config.toml"
    assert config_toml.is_file(), "codex config.toml not written"
    data = tomllib.loads(config_toml.read_text())

    servers = data.get("mcp_servers") or {}
    assert "molecule-platform" in servers, (
        "[mcp_servers.molecule-platform] was NOT rendered into the codex "
        "config.toml — the declared management MCP plugin never reached the "
        "codex CLI's native config (the codex concierge would boot without "
        f"create_workspace). Got servers: {sorted(servers)}"
    )

    entry = servers["molecule-platform"]
    assert entry.get("command") == "npx"
    assert entry.get("args") == ["-y", "@molecule-ai/mcp-server"]

    env = entry.get("env") or {}
    # Descriptor-declared key preserved.
    assert env.get("MOLECULE_MCP_MODE") == "management"
    # Literals injected by the codex register_mcp_server_hook override —
    # codex drops these from its child env whitelist otherwise.
    assert env.get("MOLECULE_CP_URL") == "https://cp.example.test"
    assert env.get("MOLECULE_ADMIN_TOKEN") == "admin-tok-xyz"
    assert env.get("PLATFORM_URL") == "http://platform:8080"
    assert env.get("WORKSPACE_ID") == "ws-codex-concierge"


@pytest.mark.asyncio
async def test_setup_does_not_write_claude_settings_for_codex(
    monkeypatch, tmp_path
):
    """The #3159 invariant: the codex render must NOT write the management MCP
    into .claude/settings.json (a file codex never reads)."""
    if not shutil.which("codex"):
        pytest.skip("codex binary not on PATH (container-only check)")

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("MOLECULE_LLM_BILLING_MODE", raising=False)

    configs = tmp_path / "configs"
    _make_platform_mcp_plugin(configs / "plugins")
    monkeypatch.setenv("PLUGINS_DIR", str(tmp_path / "no-shared-plugins"))

    from adapter import CodexAdapter
    from molecule_runtime.adapters.base import AdapterConfig

    await CodexAdapter().setup(
        AdapterConfig(model="gpt-5.5", config_path=str(configs),
                      workspace_id="ws-1")
    )

    claude_settings = configs / ".claude" / "settings.json"
    assert not claude_settings.exists(), (
        "codex setup wrote .claude/settings.json — the management MCP must "
        "land in ~/.codex/config.toml, not a Claude file codex never reads "
        "(#3159 regression)."
    )


@pytest.mark.asyncio
async def test_management_mcp_present_true_after_setup(monkeypatch, tmp_path):
    """The runtime-agnostic RCA#2970 probe sees the management MCP for codex
    after setup() — proving the online gate won't fail-close a codex
    concierge."""
    if not shutil.which("codex"):
        pytest.skip("codex binary not on PATH (container-only check)")

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("MOLECULE_LLM_BILLING_MODE", raising=False)

    configs = tmp_path / "configs"
    _make_platform_mcp_plugin(configs / "plugins")
    monkeypatch.setenv("PLUGINS_DIR", str(tmp_path / "no-shared-plugins"))

    from adapter import CodexAdapter
    from molecule_runtime.adapters.base import AdapterConfig

    adapter = CodexAdapter()
    cfg = AdapterConfig(model="gpt-5.5", config_path=str(configs),
                        workspace_id="ws-1")
    await adapter.setup(cfg)

    assert adapter.management_mcp_present(cfg) is True


def test_mcp_tool_id_from_item():
    """codex#3082 producer: an MCP-tool-call item → mcp__<server>__<tool>."""
    from executor import _mcp_tool_id_from_item

    # Canonical 0.130 shape.
    assert (
        _mcp_tool_id_from_item(
            {"type": "mcp_tool_call", "server": "molecule-platform",
             "tool": "create_workspace"}
        )
        == "mcp__molecule-platform__create_workspace"
    )
    # camelCase variant.
    assert (
        _mcp_tool_id_from_item(
            {"type": "mcpToolCall", "serverName": "molecule-platform",
             "toolName": "list_workspaces"}
        )
        == "mcp__molecule-platform__list_workspaces"
    )
    # Non-MCP items (codex builtin tool, agent message) → None.
    assert _mcp_tool_id_from_item(
        {"type": "function_call", "name": "shell"}
    ) is None
    assert _mcp_tool_id_from_item(
        {"type": "agent_message", "text": "hi"}
    ) is None
    # Malformed / partial → None (never a half-built id).
    assert _mcp_tool_id_from_item({"type": "mcp_tool_call"}) is None
    assert _mcp_tool_id_from_item(None) is None
