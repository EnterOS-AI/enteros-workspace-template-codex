#!/usr/bin/env bash
# codex_auth_sync.sh — GET-ONLY codex auth.json re-sync watchdog.
#
# Why this exists (codex shared-OAuth durable fix, 2026-05-31):
#   Multiple codex agents share ONE ChatGPT-Pro OAuth token (the platform
#   global secret CODEX_AUTH_JSON). OpenAI's refresh_token is SINGLE-USE:
#   every refresh rotates it and invalidates the prior one. When each
#   per-agent codex app-server refreshed independently on a 401 it burned
#   the shared seed within seconds (a refresh storm that wedged every agent).
#
#   The fix splits responsibilities:
#     - ONE owner rotates the refresh_token: the platform-side central
#       refresher (molecule-core internal/codexauth). It writes the rotated
#       blob back to global_secrets.
#     - EVERY workspace only ever RE-SYNCS (GET) the current token from the
#       platform and writes it to auth.json. This script. It NEVER POSTs to
#       any OAuth endpoint — token rotation is not its job.
#
#   The critical ordering: a workspace's /home/agent volume PERSISTS the old
#   auth.json across restarts. If the codex app-server starts on that STALE
#   auth.json, its first call 401s and (pre-fix) it refreshed — re-igniting
#   the burn. So this script runs on boot BEFORE the app-server starts and
#   OVERWRITES the stale auth.json with the platform's current token.
#
# What it does:
#   GET ${PLATFORM_URL}/workspaces/${WORKSPACE_ID}/secrets/values
#       Authorization: Bearer <workspace .auth_token>
#   -> extract the CODEX_AUTH_JSON value (the auth.json contents)
#   -> atomically (write-temp + chmod 0600 + rename) write it to
#      ${CODEX_HOME}/auth.json, owned by the agent.
#
#   Runs: once on boot (synchronously, before the app-server), then every
#   ${CODEX_AUTH_SYNC_INTERVAL_SECONDS} (default 1h). `--once` does a single
#   sync and exits.
#
# Inert / skip (rc=1, auth.json untouched) when:
#   - no CODEX_HOME (nothing to sync into), OR
#   - WORKSPACE_ID / PLATFORM_URL / .auth_token cannot be resolved, OR
#   - the platform response carries no CODEX_AUTH_JSON (this workspace is not
#     a shared-codex-subscription workspace — e.g. plain OPENAI_API_KEY).
#
# Transient failure (rc=2, auth.json untouched): platform 401/5xx/network.
# The loop retries next interval; on boot start.sh logs but does not fail.
#
# It NEVER echoes token values — filenames + status codes + field NAMES only.

set -uo pipefail

CODEX_HOME="${CODEX_HOME:-/home/agent/.codex}"
AUTH_JSON="${CODEX_HOME}/auth.json"

# Resolve python3 portably — the SAME robust logic codex_auth_refresh.sh uses
# (PR#24: the image is python:3.11-slim with python3 at /usr/local/bin; a
# hardcoded /opt/molecule-venv path exits 127 and silently disables the
# watchdog). Override via CODEX_PYTHON for test/dev rigs.
PYTHON_BIN="${CODEX_PYTHON:-$(command -v python3 || true)}"
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
  printf '[codex_auth_sync %s] FATAL: python3 not found on PATH (CODEX_PYTHON=%s); the sync watchdog cannot run.\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${CODEX_PYTHON:-<unset>}" >&2
  exit 127
fi

SYNC_INTERVAL="${CODEX_AUTH_SYNC_INTERVAL_SECONDS:-3600}"

# Platform routing — mirror codex_mcp_config.sh / a2a_client.py defaults.
PLATFORM_URL_VAL="${WORKSPACE_SERVER_URL:-${PLATFORM_URL:-http://platform:8080}}"
# Org routing header — the SaaS tenant API requires X-Molecule-Org-Id matching the
# org UUID (TENANT_ORG_HEADER_REQUIRED); without it the re-sync GET 400s.
ORG_ID_VAL="${MOLECULE_ORG_ID:-${ORG_ID:-}}"
WORKSPACE_ID_VAL="${WORKSPACE_ID:-}"

log() {
  printf '[codex_auth_sync %s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

# Resolve the directory the runtime persists .auth_token into — identical
# resolution to codex_mcp_config.sh so we read the SAME token the runtime
# wrote. Explicit CONFIGS_DIR wins; else ask the runtime's own
# configs_dir.resolve(); else fall back to the agent-home path.
_resolve_configs_dir() {
  if [ -n "${CONFIGS_DIR:-}" ]; then
    printf '%s\n' "${CONFIGS_DIR}"
    return
  fi
  HOME="${HOME:-/home/agent}" "$PYTHON_BIN" - <<'PY' 2>/dev/null
import molecule_runtime.configs_dir as c
print(c.resolve())
PY
}

# Read the workspace .auth_token. Returns the token on stdout (caller MUST
# NOT echo it) or empty if absent.
_read_auth_token() {
  local cfg_dir token_file
  cfg_dir="$(_resolve_configs_dir)"
  if [ -z "$cfg_dir" ]; then
    cfg_dir="${HOME:-/home/agent}/.molecule-workspace"
  fi
  token_file="${cfg_dir}/.auth_token"
  if [ -s "$token_file" ]; then
    # Trim trailing newline/whitespace without echoing the value anywhere.
    tr -d '\r\n' < "$token_file"
  fi
}

# One sync attempt. rc: 0 synced (auth.json written), 1 skip (inert / not a
# shared-codex workspace / missing routing), 2 transient failure.
attempt_sync_once() {
  if [ ! -d "$CODEX_HOME" ]; then
    log "skip: CODEX_HOME=$CODEX_HOME does not exist (nothing to sync)"
    return 1
  fi
  if [ -z "$WORKSPACE_ID_VAL" ]; then
    log "skip: WORKSPACE_ID unset — cannot resolve the secrets endpoint"
    return 1
  fi

  local auth_token
  auth_token="$(_read_auth_token)"
  if [ -z "$auth_token" ]; then
    log "skip: no workspace .auth_token resolved — cannot authenticate the re-sync GET"
    return 1
  fi

  local url response_file http_code
  url="${PLATFORM_URL_VAL%/}/workspaces/${WORKSPACE_ID_VAL}/secrets/values"
  response_file="$(mktemp)"

  # GET ONLY. We never POST to any OAuth endpoint — token rotation is the
  # platform central refresher's job. The bearer token goes in a header (not
  # argv), the response body to a temp file.
  http_code="$(
    curl -sS --max-time 30 \
      -H "Authorization: Bearer ${auth_token}" \
      -H "X-Molecule-Org-Id: ${ORG_ID_VAL}" \
      -H "Accept: application/json" \
      -X GET \
      -o "$response_file" \
      -w "%{http_code}" \
      "$url" \
      || echo "000"
  )"
  unset auth_token

  case "$http_code" in
    2*) : ;;
    401|403)
      log "transient: re-sync GET unauthorized (http=$http_code) — workspace token may be mid-rotation; will retry"
      rm -f "$response_file"
      return 2
      ;;
    *)
      log "transient: re-sync GET failed (http=$http_code); will retry next interval"
      rm -f "$response_file"
      return 2
      ;;
  esac

  # Extract CODEX_AUTH_JSON from the {"KEY":"value"} secrets map and write it
  # atomically to auth.json (0600). The helper prints a status word on stdout:
  #   WROTE          - auth.json rewritten from the platform current token
  #   SKIP_NO_KEY    - response has no CODEX_AUTH_JSON (not a shared-codex ws)
  #   SKIP_NOOP      - platform token already equal to auth.json
  #   FAIL <reason>  - malformed response / write error
  #
  # The helper body is written to a temp .py file and executed, rather than
  # an inline heredoc inside $(...): a heredoc nested in command substitution
  # mis-parses under bash 3.2 (the build smoke and some dev hosts), and
  # codex_auth_refresh.sh deliberately avoids that shape too.
  local helper_py verdict
  helper_py="$(mktemp /tmp/codex_auth_sync.helper.XXXXXX.py)"
  cat > "$helper_py" <<'PY'
import json, os, stat, sys, tempfile

response_path = os.environ["CODEX_AUTH_JSON_RESPONSE_FILE"]
target = os.environ["CODEX_AUTH_JSON_TARGET"]

try:
    with open(response_path, "r", encoding="utf-8") as f:
        secrets = json.load(f)
except (OSError, json.JSONDecodeError) as exc:
    print("FAIL bad_response_json:" + type(exc).__name__)
    sys.exit(0)

if not isinstance(secrets, dict):
    print("FAIL response_not_object")
    sys.exit(0)

blob = secrets.get("CODEX_AUTH_JSON")
if blob is None or (isinstance(blob, str) and blob.strip() == ""):
    print("SKIP_NO_KEY")
    sys.exit(0)

# The secrets-map value is the auth.json CONTENTS (a JSON string). Validate it
# parses as JSON before adopting it - never overwrite a good auth.json with
# garbage.
if isinstance(blob, (dict, list)):
    new_text = json.dumps(blob)
else:
    new_text = str(blob)
    try:
        json.loads(new_text)
    except json.JSONDecodeError:
        print("FAIL codex_auth_json_not_json")
        sys.exit(0)

# No-op if the on-disk auth.json already equals the platform current token
# (semantic compare so formatting differences do not force a rewrite).
try:
    with open(target, "r", encoding="utf-8") as f:
        current = f.read()
    if json.loads(current) == json.loads(new_text):
        print("SKIP_NOOP")
        sys.exit(0)
except (OSError, json.JSONDecodeError):
    pass  # absent / unreadable / stale -> proceed to overwrite

# Atomic write: temp + fsync + chmod 0600 + rename.
dir_path = os.path.dirname(target) or "."
fd, tmp_path = tempfile.mkstemp(prefix=".auth.sync.", suffix=".tmp", dir=dir_path)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    os.replace(tmp_path, target)
except Exception as exc:  # noqa: BLE001
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    print("FAIL write:" + type(exc).__name__)
    sys.exit(0)

print("WROTE")
PY
  verdict="$(
    CODEX_AUTH_JSON_RESPONSE_FILE="$response_file" \
    CODEX_AUTH_JSON_TARGET="$AUTH_JSON" \
    "$PYTHON_BIN" "$helper_py" 2>>/tmp/codex_auth_sync.errlog
  )"
  rm -f "$response_file" "$helper_py"

  case "$verdict" in
    WROTE)
      # Ensure agent ownership (the helper runs as whoever launched the
      # script; start.sh launches it as gosu agent, but be defensive).
      chown agent:agent "$AUTH_JSON" 2>/dev/null || true
      log "synced: rewrote ${AUTH_JSON} from the platform's current CODEX_AUTH_JSON (0600 agent)"
      return 0
      ;;
    SKIP_NOOP)
      log "no-op: ${AUTH_JSON} already matches the platform's current token"
      return 0
      ;;
    SKIP_NO_KEY)
      log "skip: platform response carries no CODEX_AUTH_JSON (not a shared-codex-subscription workspace)"
      return 1
      ;;
    FAIL*)
      log "transient: ${verdict#FAIL } (auth.json untouched); will retry next interval"
      return 2
      ;;
    *)
      log "transient: unrecognized sync helper verdict (len=${#verdict}); auth.json untouched"
      return 2
      ;;
  esac
}

main_loop() {
  log "watchdog: starting (interval=${SYNC_INTERVAL}s codex_home=${CODEX_HOME} platform=${PLATFORM_URL_VAL} workspace_id=${WORKSPACE_ID_VAL:-<unset>})"
  while true; do
    attempt_sync_once || true
    sleep "$SYNC_INTERVAL"
  done
}

case "${1:-}" in
  --once)
    attempt_sync_once
    exit $?
    ;;
  --help|-h)
    grep '^#' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *)
    main_loop
    ;;
esac
