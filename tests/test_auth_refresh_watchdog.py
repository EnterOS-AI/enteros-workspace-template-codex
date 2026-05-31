"""Tests for codex_auth_refresh.sh (RFC internal#569).

The watchdog is a bash script with inline Python helpers; we exercise it
end-to-end with `--once`, mocking the OAuth refresh endpoint via a
local HTTP server. This catches: JWT exp parsing, last_refresh aging,
the atomic-rename + 0600 chmod, the auth_mode gate (subscription-only),
and the 401-permanent vs network-transient split.

CRITICAL: every assertion that touches token-shaped strings checks them
by hash/length, never by literal value — so the test logs themselves
never embed token contents. The watchdog's "no plaintext tokens in
output" invariant is asserted by scanning combined stderr/stdout.
"""
from __future__ import annotations

import base64
import http.server
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

# Path to the script under test.
_SCRIPT = Path(__file__).resolve().parent.parent / "codex_auth_refresh.sh"

# The script now resolves python3 portably via `command -v python3`,
# with a CODEX_PYTHON override for test rigs that want to force a
# specific interpreter (e.g., a venv interpreter that matches the test
# process). We pass the active interpreter explicitly so the inline
# heredocs run under the same Python the test harness uses — important
# when the runner's system python3 differs from the venv python.
_REAL_PY = sys.executable


def _make_jwt(claims: Dict) -> str:
    """Encode a JWT-shaped string. Signature is junk — the watchdog
    only inspects the payload `exp` claim, not the signature."""
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps(claims).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.signature_unused_in_watchdog"


def _write_auth_json(
    path: Path,
    *,
    auth_mode: str = "chatgpt",
    access_token_exp_offset: int = 3600 * 24,  # 24h from now
    last_refresh_iso: Optional[str] = None,
    refresh_token: str = "RT_SENTINEL_DO_NOT_LOG_THIS_VALUE",
    access_token: Optional[str] = None,
) -> None:
    if access_token is None:
        if access_token_exp_offset is None:
            access_token = "AT_SENTINEL_NO_EXP_CLAIM"
        else:
            access_token = _make_jwt(
                {"exp": int(time.time()) + access_token_exp_offset}
            )
    blob: Dict = {
        "auth_mode": auth_mode,
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "ID_SENTINEL_DO_NOT_LOG_THIS_VALUE",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": "acct_test",
        },
        "last_refresh": last_refresh_iso,
    }
    path.write_text(json.dumps(blob), encoding="utf-8")
    path.chmod(0o600)


class _MockOAuthHandler(http.server.BaseHTTPRequestHandler):
    # Class-level state — set per-test.
    response_status: int = 200
    response_body: bytes = b""
    received_body: Optional[bytes] = None

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        _MockOAuthHandler.received_body = self.rfile.read(length)
        self.send_response(_MockOAuthHandler.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_MockOAuthHandler.response_body)))
        self.end_headers()
        self.wfile.write(_MockOAuthHandler.response_body)

    def log_message(self, *args, **kwargs) -> None:
        return


def _start_mock_server(
    status: int, body: Dict | bytes
) -> Tuple[http.server.HTTPServer, str, threading.Thread]:
    _MockOAuthHandler.response_status = status
    if isinstance(body, dict):
        _MockOAuthHandler.response_body = json.dumps(body).encode()
    else:
        _MockOAuthHandler.response_body = body
    _MockOAuthHandler.received_body = None
    # Bind to localhost, ephemeral port.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.HTTPServer(("127.0.0.1", port), _MockOAuthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/oauth/token"
    return server, url, thread


def _run_once(
    codex_home: Path,
    extra_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "CODEX_HOME": str(codex_home),
        "HOME": str(codex_home.parent),
        # Force the watchdog's inline-python helpers to use the same
        # interpreter as the test harness so tests don't depend on
        # whatever `command -v python3` happens to resolve to on the
        # CI runner.
        "CODEX_PYTHON": _REAL_PY,
        # OWNER GATE (codex shared-OAuth durable fix, 2026-05-31): the per-agent
        # OAuth POST is DISABLED unless CODEX_AUTH_REFRESH_OWNER=1. The template
        # never sets it (workspaces re-sync via codex_auth_sync.sh; rotation is
        # the platform central refresher's job). These rotation tests assert the
        # OWNER path, so they opt in explicitly. The owner-gate test below runs
        # WITHOUT this var to prove the default-off behavior.
        "CODEX_AUTH_REFRESH_OWNER": "1",
        # Short timeouts so the test suite runs in seconds, not hours.
        "CODEX_AUTH_REFRESH_INTERVAL_SECONDS": "30",
        "CODEX_AUTH_SAFETY_MARGIN_SECONDS": "14400",
        "CODEX_AUTH_STALE_AFTER_SECONDS": "604800",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(_SCRIPT), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _assert_no_token_in_output(result: subprocess.CompletedProcess) -> None:
    """Watchdog invariant: token values never appear in logs."""
    combined = result.stdout + result.stderr
    for sentinel in (
        "RT_SENTINEL_DO_NOT_LOG_THIS_VALUE",
        "ID_SENTINEL_DO_NOT_LOG_THIS_VALUE",
        "AT_SENTINEL_NO_EXP_CLAIM",
    ):
        assert sentinel not in combined, (
            f"watchdog leaked token sentinel {sentinel!r} into output"
        )


def test_script_does_not_exit_127_with_portable_python_path(tmp_path: Path) -> None:
    """Regression pin for the PR#19 → PR#24 chain: the watchdog hardcoded
    /opt/molecule-venv/bin/python3 which does not exist in the codex
    image (built FROM python:3.11-slim; python3 lives at
    /usr/local/bin/python3). Every helper invocation exited 127 → OAuth
    refresh never fired → id_token expired silently → Researcher wedged
    (ae2c3012 diagnosis). The portable-python-path fix means the script
    must NEVER exit 127, even with no CODEX_PYTHON override and an
    absent auth.json. If this test fails the image is shipping broken
    OAuth refresh again.
    """
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    # Deliberately omit CODEX_PYTHON so the test exercises the same
    # auto-detect path the production image relies on.
    env = {
        **os.environ,
        "CODEX_HOME": str(codex_home),
        "HOME": str(codex_home.parent),
    }
    # Strip CODEX_PYTHON from any inherited environment so this test
    # genuinely exercises `command -v python3`.
    env.pop("CODEX_PYTHON", None)
    result = subprocess.run(
        ["bash", str(_SCRIPT), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 127, (
        "codex_auth_refresh.sh exited 127 — python3 helper not located. "
        f"stderr={result.stderr!r}"
    )
    # No auth.json present → expect the skip path (rc=1) plus the
    # specific skip log line. This also confirms the script actually
    # executed past the python-resolve step into the main flow.
    assert result.returncode == 1, (
        f"expected skip rc=1 with no auth.json; got rc={result.returncode}\n"
        f"stderr={result.stderr}"
    )
    assert "absent or empty" in result.stderr


def test_skip_when_auth_json_absent(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    result = _run_once(codex_home)
    assert result.returncode == 1  # skipped, not error
    assert "absent or empty" in result.stderr


def test_skip_when_auth_mode_is_not_chatgpt(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    _write_auth_json(codex_home / "auth.json", auth_mode="api_key")
    result = _run_once(codex_home)
    assert result.returncode == 1
    assert "auth_mode=api_key" in result.stderr
    _assert_no_token_in_output(result)


def test_skip_when_token_fresh(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    # exp 24h away, last_refresh recent → no refresh needed.
    now_iso = (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))
    )
    _write_auth_json(
        codex_home / "auth.json",
        access_token_exp_offset=3600 * 24,
        last_refresh_iso=now_iso,
    )
    result = _run_once(codex_home)
    assert result.returncode == 1
    assert "no-op" in result.stderr or "SKIP fresh" in result.stderr
    _assert_no_token_in_output(result)
    # Sidecar should be written even on skip.
    status = json.loads((codex_home / "auth_refresh_status.json").read_text())
    assert status["watchdog_last_outcome"] == "skip"


def test_refresh_when_access_token_expired_atomically_rewrites(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    # exp 1h IN THE PAST → forces refresh.
    _write_auth_json(
        codex_home / "auth.json",
        access_token_exp_offset=-3600,
    )
    new_access = _make_jwt({"exp": int(time.time()) + 3600 * 24 * 8})
    new_id = _make_jwt({"exp": int(time.time()) + 3600 * 24})
    server, url, _ = _start_mock_server(
        200,
        {
            "id_token": new_id,
            "access_token": new_access,
            "refresh_token": "NEW_RT_FROM_SERVER",
        },
    )
    try:
        result = _run_once(
            codex_home, extra_env={"CODEX_REFRESH_TOKEN_URL_OVERRIDE": url}
        )
    finally:
        server.shutdown()

    assert result.returncode == 0, (
        f"expected refresh ok; got rc={result.returncode}\nstderr={result.stderr}"
    )
    _assert_no_token_in_output(result)

    # Verify the new auth.json was atomically written.
    new_blob = json.loads((codex_home / "auth.json").read_text())
    assert new_blob["tokens"]["access_token"] == new_access
    assert new_blob["tokens"]["id_token"] == new_id
    assert new_blob["tokens"]["refresh_token"] == "NEW_RT_FROM_SERVER"
    # last_refresh updated.
    assert new_blob["last_refresh"]
    # 0600 mode preserved.
    mode = stat.S_IMODE((codex_home / "auth.json").stat().st_mode)
    assert mode == 0o600, f"auth.json mode is {oct(mode)}, expected 0o600"
    # Sidecar reflects success.
    status = json.loads((codex_home / "auth_refresh_status.json").read_text())
    assert status["watchdog_last_outcome"] == "refreshed"

    # Verify the request body sent to the mock — confirms vendor
    # contract is honored (client_id + grant_type + refresh_token).
    req = json.loads(_MockOAuthHandler.received_body or b"{}")
    assert req["client_id"] == "app_EMoamEEZ73f0CkXaXp7hrann"
    assert req["grant_type"] == "refresh_token"
    assert req["refresh_token"] == "RT_SENTINEL_DO_NOT_LOG_THIS_VALUE"


def test_refresh_treats_401_as_permanent(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    _write_auth_json(
        codex_home / "auth.json", access_token_exp_offset=-3600
    )
    server, url, _ = _start_mock_server(
        401,
        {"error": {"code": "refresh_token_expired"}},
    )
    try:
        result = _run_once(
            codex_home, extra_env={"CODEX_REFRESH_TOKEN_URL_OVERRIDE": url}
        )
    finally:
        server.shutdown()

    assert result.returncode == 3, f"expected permanent failure rc=3; got {result.returncode}\nstderr={result.stderr}"
    assert "PERMANENT" in result.stderr
    _assert_no_token_in_output(result)
    # auth.json untouched on failure.
    blob = json.loads((codex_home / "auth.json").read_text())
    assert blob["tokens"]["refresh_token"] == "RT_SENTINEL_DO_NOT_LOG_THIS_VALUE"


def test_refresh_treats_5xx_as_transient(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    _write_auth_json(
        codex_home / "auth.json", access_token_exp_offset=-3600
    )
    server, url, _ = _start_mock_server(503, b"upstream unavailable")
    try:
        result = _run_once(
            codex_home, extra_env={"CODEX_REFRESH_TOKEN_URL_OVERRIDE": url}
        )
    finally:
        server.shutdown()

    assert result.returncode == 2, f"expected transient rc=2; got {result.returncode}\nstderr={result.stderr}"
    assert "transient" in result.stderr
    _assert_no_token_in_output(result)
    # auth.json untouched on transient failure (CLI semantics:
    # manager.rs:849-854).
    blob = json.loads((codex_home / "auth.json").read_text())
    assert blob["tokens"]["refresh_token"] == "RT_SENTINEL_DO_NOT_LOG_THIS_VALUE"


def test_stale_last_refresh_triggers_refresh_even_with_fresh_jwt(tmp_path: Path) -> None:
    """Per CLI source manager.rs:1786-1808, last_refresh older than 8d
    is stale regardless of JWT exp. Our watchdog uses 7d (1d safety
    floor below the CLI's cliff)."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    # JWT exp is far future, but last_refresh is 10 days old.
    stale_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600 * 24 * 10)
    )
    _write_auth_json(
        codex_home / "auth.json",
        access_token_exp_offset=3600 * 24 * 30,
        last_refresh_iso=stale_iso,
    )
    server, url, _ = _start_mock_server(
        200,
        {"access_token": _make_jwt({"exp": int(time.time()) + 3600 * 24})},
    )
    try:
        result = _run_once(
            codex_home, extra_env={"CODEX_REFRESH_TOKEN_URL_OVERRIDE": url}
        )
    finally:
        server.shutdown()

    assert result.returncode == 0, f"expected refresh on stale last_refresh; rc={result.returncode}\nstderr={result.stderr}"
    _assert_no_token_in_output(result)


def test_owner_gate_default_off_no_oauth_post_auth_json_untouched(tmp_path: Path) -> None:
    """Codex shared-OAuth durable fix (2026-05-31): the per-agent OAuth POST is
    DISABLED unless CODEX_AUTH_REFRESH_OWNER=1. The template never sets it, so a
    workspace whose token is DUE for refresh must NOT POST and must leave
    auth.json byte-identical — the platform central refresher owns rotation.

    This is the anti-storm guarantee at the template layer: N agents sharing one
    single-use refresh_token cannot each rotate it (which would invalidate the
    siblings). Without the owner flag, the watchdog skips with a clear reason.
    """
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    # exp 1h IN THE PAST → would force a refresh if the owner gate were open.
    auth_path = codex_home / "auth.json"
    _write_auth_json(auth_path, access_token_exp_offset=-3600)
    before = auth_path.read_text()

    # Stand up a mock OAuth server and assert it is NEVER hit.
    server, url, _ = _start_mock_server(
        200, {"access_token": _make_jwt({"exp": int(time.time()) + 3600})}
    )
    try:
        # Run WITHOUT CODEX_AUTH_REFRESH_OWNER (strip it from inherited env).
        env = {
            **os.environ,
            "CODEX_HOME": str(codex_home),
            "HOME": str(codex_home.parent),
            "CODEX_PYTHON": _REAL_PY,
            "CODEX_REFRESH_TOKEN_URL_OVERRIDE": url,
        }
        env.pop("CODEX_AUTH_REFRESH_OWNER", None)
        result = subprocess.run(
            ["bash", str(_SCRIPT), "--once"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()

    # Skipped (rc=1), with the not-owner reason; no OAuth POST was made.
    assert result.returncode == 1, (
        f"expected skip rc=1 (not refresh owner); got rc={result.returncode}\n"
        f"stderr={result.stderr}"
    )
    assert "CODEX_AUTH_REFRESH_OWNER" in result.stderr or "not_refresh_owner" in (
        result.stderr + result.stdout
    )
    assert _MockOAuthHandler.received_body is None, (
        "owner gate off but an OAuth POST was made — the per-agent refresh storm "
        "guard is broken"
    )
    # auth.json byte-identical.
    assert auth_path.read_text() == before, "auth.json was modified despite owner gate being off"
    _assert_no_token_in_output(result)
