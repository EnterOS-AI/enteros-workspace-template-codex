"""PR-1 tests: model-roster refresh + ChatGPT-subscription auth wiring.

These cover the bounded, lower-risk PR-1 surface (RFC
``rfcs/codex-template-openai-modernization-and-chatgpt-headless-auth.md``
§4, §5, §7). They deliberately do NOT exercise the codex app-server
protocol — the 0.130 version bump + executor C1/C2/C3 changes are
sequenced into PR-2 with its own round-trip gate.

Three groups:
  1. config.yaml model roster is the verified May-2026 set + default.
  2. adapter.setup() accepts auth.json as a third credential (mode C)
     and still fails closed when nothing is set.
  3. start.sh writes/omits ~/.codex/auth.json + config.toml keys
     correctly based on CODEX_CHATGPT_AUTH_JSON (structural; mode C is
     verified structurally only — we do not hold a real CTO auth.json).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Verified May-2026 codex roster (RFC §1, §9 — OpenAI Codex Models +
# Configuration Reference; live-probed thread/start default = gpt-5.5).
_VALID_MAY_2026_IDS = {
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.2",
}
# Ids that were in the stale template and are NOT valid (RFC §1).
_DEAD_IDS = {"gpt-5", "gpt-5-mini", "o4-mini", "gpt-4o"}


def _load_config() -> dict:
    yaml = pytest.importorskip("yaml")
    with open(_ROOT / "config.yaml") as fh:
        return yaml.safe_load(fh)


# --- Group 1: model roster -------------------------------------------------

def test_default_model_is_gpt_5_5() -> None:
    cfg = _load_config()
    assert cfg["runtime_config"]["model"] == "gpt-5.5"


def test_roster_is_exactly_the_verified_may_2026_set() -> None:
    cfg = _load_config()
    ids = {m["id"] for m in cfg["runtime_config"]["models"]}
    assert ids == _VALID_MAY_2026_IDS, (
        f"roster {ids} != verified May-2026 set {_VALID_MAY_2026_IDS}"
    )


def test_no_dead_ids_remain() -> None:
    cfg = _load_config()
    ids = {m["id"] for m in cfg["runtime_config"]["models"]}
    assert not (ids & _DEAD_IDS), f"dead ids still present: {ids & _DEAD_IDS}"
    assert cfg["runtime_config"]["model"] not in _DEAD_IDS


def test_every_model_has_a_name_and_required_env() -> None:
    cfg = _load_config()
    for m in cfg["runtime_config"]["models"]:
        assert m.get("name"), f"model {m} missing name"
        assert m.get("required_env") == ["OPENAI_API_KEY"]


# --- Group 2: adapter preflight (mode C) -----------------------------------

@pytest.fixture()
def _adapter():
    pytest.importorskip("molecule_runtime.adapters.base")
    from adapter import CodexAdapter
    return CodexAdapter()


def _clear_creds(monkeypatch) -> None:
    for k in ("OPENAI_API_KEY", "MINIMAX_API_KEY"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.asyncio
async def test_setup_accepts_auth_json_only(_adapter, monkeypatch, tmp_path):
    """Mode C: no env keys, but a non-empty auth.json present -> passes."""
    if not shutil.which("codex"):
        pytest.skip("codex binary not on PATH (container-only check)")
    _clear_creds(monkeypatch)
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text('{"auth_mode":"chatgpt"}')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    from molecule_runtime.adapters.base import AdapterConfig
    # Should NOT raise.
    await _adapter.setup(AdapterConfig(model="gpt-5.5"))


@pytest.mark.asyncio
async def test_setup_fails_closed_with_no_credential(
    _adapter, monkeypatch, tmp_path
):
    if not shutil.which("codex"):
        pytest.skip("codex binary not on PATH (container-only check)")
    _clear_creds(monkeypatch)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty"))
    from molecule_runtime.adapters.base import AdapterConfig
    with pytest.raises(RuntimeError, match="No codex credential"):
        await _adapter.setup(AdapterConfig(model="gpt-5.5"))


@pytest.mark.asyncio
async def test_setup_ignores_empty_auth_json(_adapter, monkeypatch, tmp_path):
    """A zero-byte auth.json must NOT satisfy preflight."""
    if not shutil.which("codex"):
        pytest.skip("codex binary not on PATH (container-only check)")
    _clear_creds(monkeypatch)
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("")  # empty
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    from molecule_runtime.adapters.base import AdapterConfig
    with pytest.raises(RuntimeError, match="No codex credential"):
        await _adapter.setup(AdapterConfig(model="gpt-5.5"))


# --- Group 3: start.sh mode-C structural behavior --------------------------
# We can't run the full start.sh (it execs molecule-runtime). Instead we
# extract the mode-C block and run it in isolation with a fake HOME, then
# assert the on-disk shape. This proves the auth.json/config.toml wiring
# without the OPENAI_API_KEY / MiniMax paths changing.

_MODE_C_PROBE = r"""
set -euo pipefail
mkdir -p /home/agent && export HOME=/home/agent
# Inline the exact mode-C block from start.sh.
if [ -n "${CODEX_CHATGPT_AUTH_JSON:-}" ]; then
  CODEX_HOME_DIR="/home/agent/.codex"
  mkdir -p "$CODEX_HOME_DIR"
  AUTH_JSON_PATH="${CODEX_HOME_DIR}/auth.json"
  printf '%s' "${CODEX_CHATGPT_AUTH_JSON}" > "$AUTH_JSON_PATH"
  chmod 0600 "$AUTH_JSON_PATH"
  CONFIG_TOML="${CODEX_HOME_DIR}/config.toml"
  touch "$CONFIG_TOML"
  if ! grep -qE '^[[:space:]]*cli_auth_credentials_store[[:space:]]*=' "$CONFIG_TOML"; then
    printf 'cli_auth_credentials_store = "file"\n' >> "$CONFIG_TOML"
  fi
  if ! grep -qE '^[[:space:]]*forced_login_method[[:space:]]*=' "$CONFIG_TOML"; then
    printf 'forced_login_method = "chatgpt"\n' >> "$CONFIG_TOML"
  fi
fi
"""


def _start_sh_has_mode_c() -> bool:
    txt = (_ROOT / "start.sh").read_text()
    return "CODEX_CHATGPT_AUTH_JSON" in txt and "cli_auth_credentials_store" in txt


def test_start_sh_contains_mode_c_block() -> None:
    """Guard: the real start.sh carries the mode-C wiring + the
    single-runner intent + the preflight third-credential branch."""
    txt = (_ROOT / "start.sh").read_text()
    assert "CODEX_CHATGPT_AUTH_JSON" in txt
    assert 'cli_auth_credentials_store = "file"' in txt
    assert 'forced_login_method = "chatgpt"' in txt
    assert "single-runner" in txt.lower()
    # preflight must also accept auth.json as the third credential
    assert ".codex/auth.json" in txt


def _run_probe(env: dict) -> Path:
    home = Path(env["__TMP_HOME"])
    script = _MODE_C_PROBE.replace("/home/agent", str(home))
    runenv = {**os.environ, **{k: v for k, v in env.items()
                               if not k.startswith("__")}}
    subprocess.run(
        ["bash", "-c", script], env=runenv, check=True,
        capture_output=True, text=True,
    )
    return home / ".codex"


def test_mode_c_writes_auth_json_and_config_keys(tmp_path) -> None:
    codex_dir = _run_probe({
        "CODEX_CHATGPT_AUTH_JSON": '{"auth_mode":"chatgpt","tokens":{}}',
        "__TMP_HOME": str(tmp_path),
    })
    auth = codex_dir / "auth.json"
    toml = codex_dir / "config.toml"
    assert auth.read_text() == '{"auth_mode":"chatgpt","tokens":{}}'
    mode = oct(auth.stat().st_mode & 0o777)
    assert mode == "0o600", f"auth.json perms {mode} != 0o600"
    body = toml.read_text()
    assert 'cli_auth_credentials_store = "file"' in body
    assert 'forced_login_method = "chatgpt"' in body


def test_mode_c_is_inert_when_env_unset(tmp_path) -> None:
    codex_dir = _run_probe({"__TMP_HOME": str(tmp_path)})
    assert not (codex_dir / "auth.json").exists()
    assert not (codex_dir / "config.toml").exists()


def test_mode_c_does_not_duplicate_config_keys(tmp_path) -> None:
    """Idempotent: a pre-existing key (e.g. from the minimax helper)
    must not be appended a second time."""
    home = tmp_path
    cdir = home / ".codex"
    cdir.mkdir()
    (cdir / "config.toml").write_text(
        'cli_auth_credentials_store = "file"\nmodel = "x"\n'
    )
    _run_probe({
        "CODEX_CHATGPT_AUTH_JSON": "{}",
        "__TMP_HOME": str(home),
    })
    body = (cdir / "config.toml").read_text()
    assert body.count("cli_auth_credentials_store") == 1
