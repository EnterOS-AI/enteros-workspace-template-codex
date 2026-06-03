"""Regression tests for the codex sandbox network-access fix (core#2128).

Codex 0.130 runs every model-generated shell command inside an OS-level
sandbox (bwrap on Linux). On the workspace AMI kernel, codex makes bwrap
unshare the network namespace — even under ``danger-full-access`` — and
bwrap then fails to bring up loopback in the new netns:

    bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted

so every shell/git/curl/ssh call inside codex agents (the CR2 reviewer +
Researcher in agents-team) dies and the agent looks dead from the
outside, stalling the review→merge pipeline (core#2128 / internal#667).

The fix lives in ``codex_mcp_config.sh``: it selects the
``workspace-write`` sandbox policy and grants ``network_access = true``
under ``[sandbox_workspace_write]``. With network access explicitly
granted codex does NOT unshare the net namespace, so bwrap never
attempts the kernel-rejected RTM_NEWADDR.

These tests run the REAL ``codex_mcp_config.sh`` against a throwaway
``CODEX_HOME`` and assert on the generated ``config.toml`` — proving the
sandbox/network keys land, that the file is still valid TOML with
``sandbox_mode`` correctly placed as a top-level key (TOML rejects a
bare key after a ``[table]`` header), and that re-running the script is
idempotent (no duplicate keys/tables accumulate across reboots).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_CODEX_MCP_SH = _ROOT / "codex_mcp_config.sh"
_CODEX_MINIMAX_SH = _ROOT / "codex_minimax_config.sh"


def _run_mcp(tmp_path: Path, env_extra: dict | None = None) -> Path:
    """Run the real codex_mcp_config.sh against a throwaway CODEX_HOME
    and return the path to the generated config.toml."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "CODEX_HOME": str(codex_home),
        "HOME": str(tmp_path),
        "WORKSPACE_ID": "ws-sandbox-test",
        "PLATFORM_URL": "http://platform:8080",
        "MOLECULE_ORG_ID": "org-sandbox-test",
        "CONFIGS_DIR": "/configs",
        **(env_extra or {}),
    }
    subprocess.run(
        ["bash", str(_CODEX_MCP_SH)],
        env=env, check=True, capture_output=True, text=True,
    )
    return codex_home / "config.toml"


def _run_minimax(tmp_path: Path) -> None:
    """Run codex_minimax_config.sh first so config.toml already carries
    top-level model/model_provider keys + the provider table — the real
    boot order (start.sh runs minimax then mcp)."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "CODEX_HOME": str(codex_home),
        "HOME": str(tmp_path),
        "MINIMAX_API_KEY": "sk-test-sandbox-order",
        "WORKSPACE_CONFIG_PATH": str(tmp_path / "no-configs"),
    }
    subprocess.run(
        ["bash", str(_CODEX_MINIMAX_SH)],
        env=env, check=True, capture_output=True, text=True,
    )


# --- the sandbox/network keys land ----------------------------------------

def test_sandbox_mode_is_workspace_write(tmp_path) -> None:
    cfg = tomllib.loads(_run_mcp(tmp_path).read_text())
    assert cfg.get("sandbox_mode") == "workspace-write", (
        "sandbox_mode must be workspace-write so codex keeps an fs "
        "sandbox but does not unshare the net namespace (core#2128)."
    )


def test_network_access_is_enabled(tmp_path) -> None:
    cfg = tomllib.loads(_run_mcp(tmp_path).read_text())
    table = cfg.get("sandbox_workspace_write", {})
    assert table.get("network_access") is True, (
        "[sandbox_workspace_write].network_access must be true — this is "
        "what stops codex unsharing the net ns and triggering bwrap's "
        "RTM_NEWADDR failure on the workspace AMI kernel."
    )


def test_generated_config_is_valid_toml(tmp_path) -> None:
    """tomllib.loads raises on invalid TOML. Guards the load-bearing
    ordering rule: sandbox_mode is a TOP-LEVEL key, so it must appear
    BEFORE the first [table] header — a bare key after a [table] header
    is a parse error. If the script ever appended sandbox_mode after the
    [mcp_servers.molecule] tables, this would raise."""
    body = _run_mcp(tmp_path).read_text()
    tomllib.loads(body)  # raises TOMLDecodeError on bad ordering


def test_molecule_block_still_intact(tmp_path) -> None:
    """The sandbox fix must not clobber the A2A MCP wiring."""
    cfg = tomllib.loads(_run_mcp(tmp_path).read_text())
    assert "molecule" in cfg.get("mcp_servers", {}), (
        "sandbox fix dropped the [mcp_servers.molecule] block"
    )


# --- composes with the minimax provider block (real boot order) -----------

def test_composes_with_minimax_provider_block(tmp_path) -> None:
    """start.sh runs codex_minimax_config.sh (writes top-level
    model/model_provider keys + [model_providers.minimax]) BEFORE
    codex_mcp_config.sh. Prepending sandbox_mode above the existing
    top-level keys must still parse and preserve the provider block."""
    _run_minimax(tmp_path)
    cfg = tomllib.loads(_run_mcp(tmp_path).read_text())
    assert cfg.get("sandbox_mode") == "workspace-write"
    assert cfg.get("sandbox_workspace_write", {}).get("network_access") is True
    assert cfg.get("model_provider") == "minimax", (
        "minimax provider override was lost when the sandbox keys were added"
    )
    assert "minimax" in cfg.get("model_providers", {})


# --- idempotency across reboots -------------------------------------------

def test_idempotent_no_duplicate_sandbox_keys(tmp_path) -> None:
    """Re-running every boot must not accumulate duplicate sandbox_mode
    lines or [sandbox_workspace_write] tables. tomllib REJECTS a
    duplicate bare key or a redefined table, so a non-idempotent script
    would make the second parse raise. We also assert the raw line/header
    counts are exactly one as a sharper signal than the parse error."""
    cfg_path = _run_mcp(tmp_path)
    _run_mcp(tmp_path)  # second boot
    body = _run_mcp(tmp_path).read_text()  # third boot

    # Still valid TOML after repeated runs (duplicate key/table -> raise).
    cfg = tomllib.loads(body)
    assert cfg.get("sandbox_mode") == "workspace-write"
    assert cfg.get("sandbox_workspace_write", {}).get("network_access") is True

    lines = body.splitlines()
    assert sum(1 for ln in lines if ln.startswith("sandbox_mode = ")) == 1, (
        f"expected exactly one sandbox_mode line:\n{body}"
    )
    assert sum(1 for ln in lines if ln.strip() == "[sandbox_workspace_write]") == 1, (
        f"expected exactly one [sandbox_workspace_write] table:\n{body}"
    )
    # And the molecule block is still single + intact across reboots.
    assert "molecule" in cfg.get("mcp_servers", {})


def test_idempotent_composed_with_minimax(tmp_path) -> None:
    """Full real-world reboot: minimax then mcp, twice. Provider block,
    sandbox keys, and MCP block all survive and stay singular."""
    _run_minimax(tmp_path)
    _run_mcp(tmp_path)
    _run_minimax(tmp_path)  # minimax cat> overwrites — wipes sandbox keys
    body = _run_mcp(tmp_path).read_text()  # mcp re-adds them

    cfg = tomllib.loads(body)
    assert cfg.get("sandbox_mode") == "workspace-write"
    assert cfg.get("sandbox_workspace_write", {}).get("network_access") is True
    assert cfg.get("model_provider") == "minimax"
    assert sum(
        1 for ln in body.splitlines() if ln.startswith("sandbox_mode = ")
    ) == 1


if __name__ == "__main__":  # pragma: no cover - convenience runner
    sys.exit(pytest.main([__file__, "-v"]))
