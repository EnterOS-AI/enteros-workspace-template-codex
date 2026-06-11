"""Tests for codex_auth_sync.sh (codex shared-OAuth durable fix, 2026-05-31).

codex_auth_sync.sh is a GET-ONLY re-sync watchdog: it pulls the CURRENT
CODEX_AUTH_JSON from the workspace-server (GET /workspaces/<id>/secrets/values,
authed with the workspace .auth_token) and atomically rewrites
$CODEX_HOME/auth.json. It NEVER POSTs to any OAuth endpoint — token rotation is
owned by the platform central refresher (molecule-core internal/codexauth).

We exercise it end-to-end with `--once` against a local mock workspace-server
that records EVERY request's method + path, so the "GET only, never POST OAuth"
invariant is provable. Token-shaped strings are sentinels asserted by presence
in the file (not logged), and stdout/stderr is scanned to prove the watchdog
never echoes token contents.
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "codex_auth_sync.sh"
_REAL_PY = sys.executable

# Sentinel token blob the mock platform serves. Never logged by the watchdog.
_PLATFORM_AUTH_BLOB = json.dumps(
    {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "ID_SENTINEL_DO_NOT_LOG",
            "access_token": "AT_SENTINEL_DO_NOT_LOG",
            "refresh_token": "RT_SENTINEL_DO_NOT_LOG",
            "account_id": "acct_test",
        },
        "last_refresh": "2026-05-31T00:00:00Z",
    }
)
_AUTH_TOKEN = "WS_AUTH_TOKEN_SENTINEL"


class _MockWorkspaceServer(http.server.BaseHTTPRequestHandler):
    # Per-test class state.
    requests: List[Tuple[str, str]] = []  # (method, path)
    secrets_body: bytes = b"{}"
    get_status: int = 200
    require_bearer: bool = True

    def _record(self) -> None:
        _MockWorkspaceServer.requests.append((self.command, self.path))

    def _auth_ok(self) -> bool:
        if not _MockWorkspaceServer.require_bearer:
            return True
        return self.headers.get("Authorization") == f"Bearer {_AUTH_TOKEN}"

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        if not self._auth_ok():
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        status = _MockWorkspaceServer.get_status
        body = _MockWorkspaceServer.secrets_body if status == 200 else b"err"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Any non-GET verb is recorded so tests can assert it NEVER happens.
    def do_POST(self) -> None:  # noqa: N802
        self._record()
        self.send_response(405)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args, **kwargs) -> None:
        return


def _start_server(
    secrets: Optional[Dict] = None,
    *,
    get_status: int = 200,
    require_bearer: bool = True,
) -> Tuple[http.server.HTTPServer, str]:
    _MockWorkspaceServer.requests = []
    _MockWorkspaceServer.get_status = get_status
    _MockWorkspaceServer.require_bearer = require_bearer
    if secrets is None:
        secrets = {"CODEX_AUTH_JSON": _PLATFORM_AUTH_BLOB, "OPENAI_API_KEY": "sk-x"}
    _MockWorkspaceServer.secrets_body = json.dumps(secrets).encode()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.HTTPServer(("127.0.0.1", port), _MockWorkspaceServer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def _setup_workspace(tmp_path: Path, *, auth_token: Optional[str] = _AUTH_TOKEN,
                     stale_auth_json: Optional[str] = None) -> Tuple[Path, Path]:
    """Create CODEX_HOME + a CONFIGS_DIR with .auth_token. Returns (codex_home, configs_dir)."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    configs = tmp_path / ".molecule-workspace"
    configs.mkdir()
    if auth_token is not None:
        (configs / ".auth_token").write_text(auth_token + "\n", encoding="utf-8")
    if stale_auth_json is not None:
        (codex_home / "auth.json").write_text(stale_auth_json, encoding="utf-8")
    return codex_home, configs


def _run_once(codex_home: Path, configs: Path, platform_url: str,
              extra_env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "CODEX_HOME": str(codex_home),
        "HOME": str(codex_home.parent),
        "CODEX_PYTHON": _REAL_PY,
        "WORKSPACE_ID": "ws-test-1",
        "PLATFORM_URL": platform_url,
        "CONFIGS_DIR": str(configs),
        "CODEX_AUTH_SYNC_INTERVAL_SECONDS": "30",
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


def _assert_no_token_leak(result: subprocess.CompletedProcess) -> None:
    combined = result.stdout + result.stderr
    for sentinel in (
        "RT_SENTINEL_DO_NOT_LOG",
        "AT_SENTINEL_DO_NOT_LOG",
        "ID_SENTINEL_DO_NOT_LOG",
        "WS_AUTH_TOKEN_SENTINEL",
    ):
        assert sentinel not in combined, f"watchdog leaked sentinel {sentinel!r}"


def _methods(reqs: List[Tuple[str, str]]) -> set:
    return {m for m, _ in reqs}


def test_resync_adopts_platform_current_token(tmp_path: Path) -> None:
    """The re-sync overwrites a STALE persisted auth.json with the platform's
    current CODEX_AUTH_JSON (0600). This is the critical boot behavior: the
    stale data-volume token is what triggers the 401→burn, so it must be
    replaced before the app-server starts."""
    codex_home, configs = _setup_workspace(
        tmp_path, stale_auth_json='{"tokens":{"access_token":"STALE_TOKEN"}}'
    )
    server, url = _start_server()
    try:
        result = _run_once(codex_home, configs, url)
    finally:
        server.shutdown()

    assert result.returncode == 0, f"expected sync rc=0; got {result.returncode}\n{result.stderr}"
    written = json.loads((codex_home / "auth.json").read_text())
    assert written["tokens"]["refresh_token"] == "RT_SENTINEL_DO_NOT_LOG"
    assert "STALE_TOKEN" not in (codex_home / "auth.json").read_text()
    mode = stat.S_IMODE((codex_home / "auth.json").stat().st_mode)
    assert mode == 0o600, f"auth.json mode {oct(mode)}, expected 0o600"
    # The request was a GET to the secrets/values endpoint.
    assert _MockWorkspaceServer.requests, "no request reached the platform"
    method, path = _MockWorkspaceServer.requests[0]
    assert method == "GET"
    assert path == "/workspaces/ws-test-1/secrets/values"
    _assert_no_token_leak(result)


def test_resync_is_get_only_never_posts_oauth(tmp_path: Path) -> None:
    """The defining invariant: codex_auth_sync.sh ONLY GETs. It must NEVER POST
    (which is what would hit an OAuth endpoint and burn the shared seed)."""
    codex_home, configs = _setup_workspace(tmp_path)
    server, url = _start_server()
    try:
        result = _run_once(codex_home, configs, url)
    finally:
        server.shutdown()

    assert result.returncode == 0
    methods = _methods(_MockWorkspaceServer.requests)
    assert methods == {"GET"}, f"sync made non-GET requests: {_MockWorkspaceServer.requests}"
    assert "POST" not in methods


def test_resync_noop_when_already_current(tmp_path: Path) -> None:
    """When auth.json already equals the platform's current token, the sync is a
    no-op (rc=0) — it does not needlessly rewrite the file."""
    codex_home, configs = _setup_workspace(
        tmp_path, stale_auth_json=_PLATFORM_AUTH_BLOB
    )
    auth_path = codex_home / "auth.json"
    mtime_before = auth_path.stat().st_mtime_ns
    server, url = _start_server()
    try:
        result = _run_once(codex_home, configs, url)
    finally:
        server.shutdown()

    assert result.returncode == 0, f"expected no-op rc=0; got {result.returncode}\n{result.stderr}"
    assert "no-op" in result.stderr or "already matches" in result.stderr
    # File not rewritten (mtime unchanged) — semantic no-op short-circuits.
    assert auth_path.stat().st_mtime_ns == mtime_before, "auth.json rewritten on a no-op sync"


def test_resync_transient_on_platform_401(tmp_path: Path) -> None:
    """A platform 401 (e.g. the workspace token is mid-rotation) is transient:
    rc=2, auth.json untouched, no OAuth POST. The loop retries next interval."""
    # Seed a WRONG workspace token so the platform returns 401.
    codex_home, configs = _setup_workspace(
        tmp_path,
        auth_token="WRONG_TOKEN",
        stale_auth_json='{"tokens":{"access_token":"KEEP_ME"}}',
    )
    server, url = _start_server()  # require_bearer=True; only _AUTH_TOKEN is accepted
    try:
        result = _run_once(codex_home, configs, url)
    finally:
        server.shutdown()

    assert result.returncode == 2, f"expected transient rc=2 on 401; got {result.returncode}\n{result.stderr}"
    # auth.json untouched.
    assert "KEEP_ME" in (codex_home / "auth.json").read_text()
    # Never POSTed.
    assert "POST" not in _methods(_MockWorkspaceServer.requests)
    _assert_no_token_leak(result)


def test_skip_when_no_codex_auth_json_in_response(tmp_path: Path) -> None:
    """A workspace that is NOT shared-codex (no CODEX_AUTH_JSON in the secrets
    map — e.g. plain OPENAI_API_KEY) skips cleanly (rc=1), leaving any existing
    auth.json untouched."""
    codex_home, configs = _setup_workspace(
        tmp_path, stale_auth_json='{"tokens":{"access_token":"PRESERVE"}}'
    )
    server, url = _start_server(secrets={"OPENAI_API_KEY": "sk-only"})
    try:
        result = _run_once(codex_home, configs, url)
    finally:
        server.shutdown()

    assert result.returncode == 1, f"expected skip rc=1; got {result.returncode}\n{result.stderr}"
    assert "no CODEX_AUTH_JSON" in result.stderr
    assert "PRESERVE" in (codex_home / "auth.json").read_text()
    assert "POST" not in _methods(_MockWorkspaceServer.requests)


def test_skip_when_no_auth_token(tmp_path: Path) -> None:
    """Without a resolvable workspace .auth_token the sync cannot authenticate;
    it skips (rc=1) and never contacts the platform."""
    codex_home, configs = _setup_workspace(tmp_path, auth_token=None)
    server, url = _start_server()
    try:
        result = _run_once(codex_home, configs, url)
    finally:
        server.shutdown()

    assert result.returncode == 1, f"expected skip rc=1; got {result.returncode}\n{result.stderr}"
    assert "auth_token" in result.stderr
    assert _MockWorkspaceServer.requests == [], "contacted platform without a token"


def test_skip_when_codex_home_absent(tmp_path: Path) -> None:
    """No CODEX_HOME → inert skip (rc=1). This is the build-smoke path."""
    missing = tmp_path / ".codex-absent"
    configs = tmp_path / ".molecule-workspace"
    configs.mkdir()
    (configs / ".auth_token").write_text(_AUTH_TOKEN + "\n", encoding="utf-8")
    server, url = _start_server()
    try:
        result = _run_once(missing, configs, url)
    finally:
        server.shutdown()
    assert result.returncode == 1
    assert "does not exist" in result.stderr


def test_no_python_path_fails_loudly(tmp_path: Path) -> None:
    """If python3 cannot be resolved the script exits 127 (the build smoke keys
    rc=127 as a hard build failure) — never a silent no-op that would let the
    stale-token burn recur."""
    codex_home, configs = _setup_workspace(tmp_path)
    env = {
        **os.environ,
        "CODEX_HOME": str(codex_home),
        "HOME": str(codex_home.parent),
        "WORKSPACE_ID": "ws-test-1",
        "PLATFORM_URL": "http://127.0.0.1:1",
        "CONFIGS_DIR": str(configs),
        # Force an explicit unresolvable interpreter. The script checks that
        # CODEX_PYTHON points at an executable file and exits 127 if not — no
        # PATH manipulation needed (so `bash` itself stays resolvable).
        "CODEX_PYTHON": "/nonexistent/python3",
    }
    result = subprocess.run(
        ["bash", str(_SCRIPT), "--once"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 127, (
        f"expected rc=127 when python3 unresolvable; got {result.returncode}\n{result.stderr}"
    )
