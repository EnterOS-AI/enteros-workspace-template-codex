"""Regression tests for the codex sandbox fix (core#2128).

Codex 0.130 runs every model-generated shell command inside an OS-level
sandbox (bwrap on Linux). On the workspace AMI/container the sandbox
cannot initialize at all:

  * it unshares the network namespace -> ``bwrap: loopback: Failed
    RTM_NEWADDR: Operation not permitted``; and even with network access
    granted (PR #77's ``workspace-write`` + ``network_access = true``),
  * it then unshares a USER namespace and maps root to confine writes ->
    ``bwrap: setting up uid map: Permission denied``.

The second failure is environmental, not a kernel toggle: the agent runs
as an unprivileged user (uid 1000, zero caps) inside an identity-mapped
container (``uid_map 0 0 4294967295``, no /etc/subuid delegation), so a
root-mapping uid_map write is refused (verified on the live CR2 agent
2026-06-04). A write-scoped sandbox therefore cannot start here, so PR #77
is a non-fix.

The durable fix in ``codex_mcp_config.sh`` is to DISABLE codex's inner
sandbox entirely -- ``sandbox_mode = "danger-full-access"`` +
``approval_policy = "never"`` -- since the tenant container is already the
isolation boundary (same as the claude-code agents).

These tests run the REAL ``codex_mcp_config.sh`` against a throwaway
``CODEX_HOME`` and assert on the generated ``config.toml``: that the
disable-sandbox keys land as top-level keys (TOML rejects a bare key after
a ``[table]`` header), that NO ``[sandbox_workspace_write]`` table is
emitted, that re-running is idempotent, and that the strip cleans up the
``workspace-write`` shape left over from PR #77 / the older hotpatch.
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
    top-level model/model_provider keys + the provider table -- the real
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


# --- the disable-sandbox keys land ----------------------------------------

def test_sandbox_mode_is_danger_full_access(tmp_path) -> None:
    cfg = tomllib.loads(_run_mcp(tmp_path).read_text())
    assert cfg.get("sandbox_mode") == "danger-full-access", (
        "sandbox_mode must be danger-full-access: a workspace-write sandbox "
        "cannot initialize on this container (uid_map root-map is refused), "
        "so codex's inner sandbox must be disabled (core#2128)."
    )


def test_approval_policy_is_never(tmp_path) -> None:
    cfg = tomllib.loads(_run_mcp(tmp_path).read_text())
    assert cfg.get("approval_policy") == "never", (
        "approval_policy must be never so codex does not block on approvals "
        "for an autonomous agent (core#2128)."
    )


def test_no_sandbox_workspace_write_table(tmp_path) -> None:
    """With danger-full-access there is no inner sandbox, so the
    [sandbox_workspace_write] table is moot and must NOT be emitted."""
    cfg = tomllib.loads(_run_mcp(tmp_path).read_text())
    assert "sandbox_workspace_write" not in cfg, (
        "[sandbox_workspace_write] must not be emitted under "
        "danger-full-access (network_access is moot with no inner sandbox)."
    )


def test_generated_config_is_valid_toml(tmp_path) -> None:
    """tomllib.loads raises on invalid TOML. Guards the load-bearing
    ordering rule: sandbox_mode/approval_policy are TOP-LEVEL keys, so they
    must appear BEFORE the first [table] header -- a bare key after a
    [table] header is a parse error."""
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
    codex_mcp_config.sh. Prepending the sandbox keys above the existing
    top-level keys must still parse and preserve the provider block."""
    _run_minimax(tmp_path)
    cfg = tomllib.loads(_run_mcp(tmp_path).read_text())
    assert cfg.get("sandbox_mode") == "danger-full-access"
    assert cfg.get("approval_policy") == "never"
    assert "sandbox_workspace_write" not in cfg
    assert cfg.get("model_provider") == "minimax", (
        "minimax provider override was lost when the sandbox keys were added"
    )
    assert "minimax" in cfg.get("model_providers", {})


# --- idempotency across reboots -------------------------------------------

def test_idempotent_no_duplicate_sandbox_keys(tmp_path) -> None:
    """Re-running every boot must not accumulate duplicate sandbox_mode /
    approval_policy lines. tomllib REJECTS a duplicate bare key, so a
    non-idempotent script would make the second parse raise. We also assert
    the raw line counts are exactly one as a sharper signal."""
    _run_mcp(tmp_path)
    _run_mcp(tmp_path)  # second boot
    body = _run_mcp(tmp_path).read_text()  # third boot

    cfg = tomllib.loads(body)
    assert cfg.get("sandbox_mode") == "danger-full-access"
    assert cfg.get("approval_policy") == "never"
    assert "sandbox_workspace_write" not in cfg

    lines = body.splitlines()
    assert sum(1 for ln in lines if ln.startswith("sandbox_mode = ")) == 1, (
        f"expected exactly one sandbox_mode line:\n{body}"
    )
    assert sum(1 for ln in lines if ln.startswith("approval_policy = ")) == 1, (
        f"expected exactly one approval_policy line:\n{body}"
    )
    assert sum(
        1 for ln in lines if ln.strip() == "[sandbox_workspace_write]"
    ) == 0, f"no [sandbox_workspace_write] table expected:\n{body}"
    assert "molecule" in cfg.get("mcp_servers", {})


def test_idempotent_composed_with_minimax(tmp_path) -> None:
    """Full real-world reboot: minimax then mcp, twice. Provider block and
    sandbox keys all survive and stay singular."""
    _run_minimax(tmp_path)
    _run_mcp(tmp_path)
    _run_minimax(tmp_path)  # minimax cat> overwrites -- wipes sandbox keys
    body = _run_mcp(tmp_path).read_text()  # mcp re-adds them

    cfg = tomllib.loads(body)
    assert cfg.get("sandbox_mode") == "danger-full-access"
    assert cfg.get("approval_policy") == "never"
    assert cfg.get("model_provider") == "minimax"
    assert sum(
        1 for ln in body.splitlines() if ln.startswith("sandbox_mode = ")
    ) == 1


def test_migrates_off_pr77_workspace_write_shape(tmp_path) -> None:
    """An agent whose config.toml was written by PR #77 (or the hotpatch)
    carries sandbox_mode="workspace-write" + a [sandbox_workspace_write]
    table. Running the corrected script must STRIP that stale shape and
    leave only the danger-full-access keys with no leftover table -- else a
    reboot leaves a conflicting/duplicate sandbox config."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    cfg_path = codex_home / "config.toml"
    # Simulate the PR #77 / hotpatch on-disk shape on the persistent volume.
    cfg_path.write_text(
        '# Auto-generated by codex_mcp_config.sh — sandbox network fix (core#2128).\n'
        'sandbox_mode = "workspace-write"\n'
        'approval_policy = "never"\n'
        'model = "gpt-5-codex"\n'
        '\n'
        '# Auto-generated by codex_mcp_config.sh — sandbox network fix (core#2128).\n'
        '[sandbox_workspace_write]\n'
        'network_access = true\n'
    )
    body = _run_mcp(tmp_path).read_text()
    cfg = tomllib.loads(body)

    assert cfg.get("sandbox_mode") == "danger-full-access", (
        "stale workspace-write was not replaced"
    )
    assert cfg.get("approval_policy") == "never"
    assert "sandbox_workspace_write" not in cfg, (
        "stale [sandbox_workspace_write] table was not stripped"
    )
    lines = body.splitlines()
    assert sum(1 for ln in lines if ln.startswith("sandbox_mode = ")) == 1
    assert sum(1 for ln in lines if ln.startswith("approval_policy = ")) == 1
    assert sum(
        1 for ln in lines if ln.strip() == "[sandbox_workspace_write]"
    ) == 0


if __name__ == "__main__":  # pragma: no cover - convenience runner
    sys.exit(pytest.main([__file__, "-v"]))
