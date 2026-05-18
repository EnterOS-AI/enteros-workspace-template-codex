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
     correctly based on CODEX_AUTH_JSON (canonical Infisical key) and
     its CODEX_CHATGPT_AUTH_JSON backward-compat alias (structural;
     mode C is verified structurally only — we do not exercise a real
     subscription round-trip in CI).
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
CODEX_AUTH_BLOB="${CODEX_AUTH_JSON:-${CODEX_CHATGPT_AUTH_JSON:-}}"
if [ -n "${CODEX_AUTH_BLOB}" ]; then
  CODEX_HOME_DIR="/home/agent/.codex"
  mkdir -p "$CODEX_HOME_DIR"
  AUTH_JSON_PATH="${CODEX_HOME_DIR}/auth.json"
  printf '%s' "${CODEX_AUTH_BLOB}" > "$AUTH_JSON_PATH"
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
    return "CODEX_AUTH_JSON" in txt and "cli_auth_credentials_store" in txt


def test_start_sh_contains_mode_c_block() -> None:
    """Guard: the real start.sh carries the mode-C wiring + the
    single-runner intent + the preflight third-credential branch."""
    txt = (_ROOT / "start.sh").read_text()
    # Canonical Infisical key (/shared/codex-oauth key CODEX_AUTH_JSON)
    assert "CODEX_AUTH_JSON" in txt
    # backward-compat alias still recognized (PR #5 name)
    assert "CODEX_CHATGPT_AUTH_JSON" in txt
    # canonical key must take precedence over the alias
    assert '${CODEX_AUTH_JSON:-${CODEX_CHATGPT_AUTH_JSON:-}}' in txt
    assert 'cli_auth_credentials_store = "file"' in txt
    assert 'forced_login_method = "chatgpt"' in txt
    assert "single-runner" in txt.lower()
    # preflight must also accept auth.json as the third credential
    assert ".codex/auth.json" in txt


def test_codex_cli_pinned_to_0130_exact() -> None:
    """The Dockerfile must pin @openai/codex to the exact 0.130.0
    patch — the stable line that supports subscription-OAuth
    auth.json. A range pin or the legacy 0.57 line is a regression."""
    df = (_ROOT / "Dockerfile").read_text()
    assert "npm install -g @openai/codex@0.130.0" in df
    assert "@openai/codex@~0.57" not in df
    assert "@openai/codex@^0.72" not in df


# --- Group 4: wire_api regression guard (internal#513) ---------------------
# codex CLI 0.130 (baked by #219) REMOVED the `chat` WireApi variant.
# It hard-fails config.toml parsing on `wire_api = "chat"` at the line
# that holds it, BEFORE auth.json / OPENAI_API_KEY is read — so the
# codex agent loop never starts and A2A stays unanswered (the live
# prod-Reviewer / prod-Researcher blocker filed as internal#513). These
# tests fail closed if anyone reverts the value, in source OR in the
# config.toml the boot script actually generates.

_CODEX_MINIMAX_SH = _ROOT / "codex_minimax_config.sh"


def test_minimax_config_source_has_no_chat_wire_api() -> None:
    """Static guard: the generator script must never hard-write
    `wire_api = "chat"` — CLI 0.130 rejects it unconditionally."""
    src = _CODEX_MINIMAX_SH.read_text()
    # Only assignments matter; the header note quotes "chat" while
    # explaining the removal, so match the TOML assignment form.
    import re
    assigns = re.findall(r'(?m)^\s*wire_api\s*=\s*"([^"]+)"', src)
    assert assigns, "expected a wire_api assignment in the heredoc"
    for val in assigns:
        assert val != "chat", (
            "codex_minimax_config.sh writes wire_api = \"chat\"; CLI "
            "0.130 hard-fails config parse on it (internal#513). Use "
            '"responses".'
        )
        assert val == "responses", (
            f'wire_api = "{val}" is not a CLI-0.130 parse-valid value; '
            'only "responses" remains after the chat-wire removal.'
        )


def test_generated_config_toml_wire_api_is_responses(tmp_path) -> None:
    """End-to-end guard: actually run codex_minimax_config.sh with a
    MiniMax key set and assert the GENERATED config.toml carries a
    CLI-0.130-valid wire_api (no `chat`, exactly `responses`). This is
    the line the live error pointed at (config.toml:11:12)."""
    codex_home = tmp_path / ".codex"
    env = {
        **os.environ,
        "MINIMAX_API_KEY": "sk-test-regression-guard",
        "CODEX_HOME": str(codex_home),
        "HOME": str(tmp_path),
        # /configs patch is best-effort + skipped when absent; point it
        # at a non-existent dir so the script's guarded branch no-ops.
        "WORKSPACE_CONFIG_PATH": str(tmp_path / "no-configs"),
    }
    subprocess.run(
        ["bash", str(_CODEX_MINIMAX_SH)],
        env=env, check=True, capture_output=True, text=True,
    )
    body = (codex_home / "config.toml").read_text()
    import re
    assigns = re.findall(r'(?m)^\s*wire_api\s*=\s*"([^"]+)"', body)
    assert assigns, f"no wire_api in generated config.toml:\n{body}"
    assert "chat" not in assigns, (
        "generated config.toml still has wire_api = \"chat\" — codex "
        f"CLI 0.130 will hard-fail parse (internal#513).\n{body}"
    )
    assert assigns == ["responses"], (
        f"generated wire_api {assigns} != ['responses'] — only "
        '"responses" is parse-valid on CLI 0.130.'
    )


# --- Group 5: subscription provider precedence (internal#513) --------------
# The PR#10 wire_api flip made config.toml PARSE on CLI 0.130, but the
# prod Reviewer/Researcher workspaces have BOTH CODEX_AUTH_JSON (the
# #219 subscription) AND MINIMAX_API_KEY set. codex_minimax_config.sh
# (cat >) was unconditionally writing model_provider=minimax +
# base_url=https://api.minimax.io/v1, and start.sh's mode-C only
# appends auth keys (it does NOT rewrite the provider). Net: codex
# authed off the subscription but POSTed to
# https://api.minimax.io/v1/responses → live 404 on every A2A turn.
# These guards fail closed if the minimax block is ever emitted while
# a subscription credential is present.

def _gen_config(tmp_path: Path, env_extra: dict) -> str:
    """Run the real codex_minimax_config.sh and return config.toml
    text (empty string if the script wrote nothing)."""
    codex_home = tmp_path / ".codex"
    env = {
        **os.environ,
        "CODEX_HOME": str(codex_home),
        "HOME": str(tmp_path),
        "WORKSPACE_CONFIG_PATH": str(tmp_path / "no-configs"),
        **env_extra,
    }
    subprocess.run(
        ["bash", str(_CODEX_MINIMAX_SH)],
        env=env, check=True, capture_output=True, text=True,
    )
    cfg = codex_home / "config.toml"
    return cfg.read_text() if cfg.exists() else ""


def test_subscription_present_skips_minimax_block(tmp_path) -> None:
    """Prod path: CODEX_AUTH_JSON + MINIMAX_API_KEY both set. The
    minimax provider block MUST NOT be written, so codex falls back
    to its built-in subscription provider (Responses API, gpt-5.5 via
    thread/start). Fails on the old (minimax-forced) behavior."""
    body = _gen_config(tmp_path, {
        "CODEX_AUTH_JSON": '{"auth_mode":"chatgpt","tokens":{}}',
        "MINIMAX_API_KEY": "sk-cp-test-prod-both-set",
    })
    assert "model_provider = \"minimax\"" not in body, (
        "minimax provider block written while the ChatGPT/Codex "
        "subscription is present — codex will POST to "
        "api.minimax.io/v1/responses and 404 (internal#513).\n" + body
    )
    assert "api.minimax.io" not in body, (
        "base_url still points at api.minimax.io with a subscription "
        "credential present (internal#513).\n" + body
    )
    assert "codex-MiniMax-M2.7" not in body, (
        "minimax model still pinned with a subscription present.\n" + body
    )


def test_subscription_alias_also_skips_minimax_block(tmp_path) -> None:
    """The PR#5 backward-compat alias CODEX_CHATGPT_AUTH_JSON must
    also suppress the minimax block."""
    body = _gen_config(tmp_path, {
        "CODEX_CHATGPT_AUTH_JSON": '{"auth_mode":"chatgpt"}',
        "MINIMAX_API_KEY": "sk-cp-test-alias",
    })
    assert "minimax" not in body, (
        "minimax block written under the CODEX_CHATGPT_AUTH_JSON "
        "alias path (internal#513).\n" + body
    )


def test_minimax_only_still_writes_minimax_block(tmp_path) -> None:
    """Regression floor for the alt leg: with NO subscription and
    MINIMAX_API_KEY set, the minimax block must still be emitted
    (the internal#514 alt path is not removed, just subordinated)."""
    body = _gen_config(tmp_path, {"MINIMAX_API_KEY": "sk-cp-test-alt-only"})
    assert "model_provider = \"minimax\"" in body, (
        "minimax-only path no longer writes the minimax block — the "
        "internal#514 alt leg must not be removed, only subordinated "
        "to the subscription.\n" + body
    )
    # And its wire_api must remain the CLI-0.130 parse-valid value.
    import re
    assigns = re.findall(r'(?m)^\s*wire_api\s*=\s*"([^"]+)"', body)
    assert assigns == ["responses"], assigns


def test_no_credentials_writes_nothing(tmp_path) -> None:
    """No subscription, no MINIMAX_API_KEY: still a true no-op so the
    direct-OPENAI_API_KEY path sees no config.toml provider override."""
    body = _gen_config(tmp_path, {"MINIMAX_API_KEY": ""})
    assert body == "", f"expected no config.toml; got:\n{body}"


def test_subscription_then_mode_c_yields_no_provider_override(
    tmp_path,
) -> None:
    """Boot-order integration: minimax script (skips) → mode-C probe
    (appends auth keys). Final config.toml must carry the subscription
    auth keys and NO model_provider/base_url override, matching the
    verified working device-logged codex-0.130 shape."""
    # 1. minimax script with subscription present -> writes nothing.
    body = _gen_config(tmp_path, {
        "CODEX_AUTH_JSON": '{"auth_mode":"chatgpt","tokens":{}}',
        "MINIMAX_API_KEY": "sk-cp-test-integration",
    })
    assert body == "", f"minimax script should be inert here:\n{body}"
    # 2. mode-C probe appends auth keys onto the (absent) config.toml.
    codex_dir = _run_probe({
        "CODEX_AUTH_JSON": '{"auth_mode":"chatgpt","tokens":{}}',
        "__TMP_HOME": str(tmp_path),
    })
    final = (codex_dir / "config.toml").read_text()
    assert "model_provider" not in final, (
        "config.toml carries a model_provider override on the "
        "subscription path — codex must use its built-in provider "
        "(internal#513).\n" + final
    )
    assert "api.minimax.io" not in final
    assert 'forced_login_method = "chatgpt"' in final
    assert 'cli_auth_credentials_store = "file"' in final


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
    """Canonical path: CODEX_AUTH_JSON (the Infisical key)."""
    codex_dir = _run_probe({
        "CODEX_AUTH_JSON": '{"auth_mode":"chatgpt","tokens":{}}',
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


def test_mode_c_alias_still_works(tmp_path) -> None:
    """Backward-compat: the PR #5 CODEX_CHATGPT_AUTH_JSON name still
    materializes auth.json when the canonical var is unset."""
    codex_dir = _run_probe({
        "CODEX_CHATGPT_AUTH_JSON": '{"auth_mode":"chatgpt","alias":1}',
        "__TMP_HOME": str(tmp_path),
    })
    assert (codex_dir / "auth.json").read_text() == \
        '{"auth_mode":"chatgpt","alias":1}'


def test_mode_c_canonical_wins_over_alias(tmp_path) -> None:
    """If both are set, CODEX_AUTH_JSON must shadow the alias so a
    Config-tab override can supersede a stale value."""
    codex_dir = _run_probe({
        "CODEX_AUTH_JSON": '{"src":"canonical"}',
        "CODEX_CHATGPT_AUTH_JSON": '{"src":"alias"}',
        "__TMP_HOME": str(tmp_path),
    })
    assert (codex_dir / "auth.json").read_text() == '{"src":"canonical"}'


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
