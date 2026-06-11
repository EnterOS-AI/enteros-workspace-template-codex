#!/usr/bin/env bash
# codex_auth_refresh.sh — proactive OAuth refresh watchdog for the
# ChatGPT/Codex-subscription credential.
#
# Why this exists (RFC internal#569):
#   Codex CLI 0.130.0 ONLY refreshes auth.json on the demand path
#   `AuthManager::auth().await` (codex-rs/login/src/auth/manager.rs:1411).
#   In our prod-Reviewer / prod-Researcher topology the workspace can
#   sit idle (or wedge upstream in executor.py) for >8 days between
#   turns, so `auth()` is never invoked end-to-end and refresh is never
#   armed. id_token expires within hours; access_token within ~198h;
#   eventually refresh_token rolls past its own window and the workspace
#   becomes hard-broken (no recovery path, requires a CTO re-login).
#
# Strategy:
#   Background bash loop, started by start.sh as `gosu agent`. Every
#   $REFRESH_INTERVAL seconds (default 6h, RFC §"Proposed fix" item 2)
#   it inspects $CODEX_HOME/auth.json. If either:
#     - tokens.access_token JWT `exp` is within $SAFETY_MARGIN seconds
#       of now (default 4h), OR
#     - last_refresh ISO timestamp is older than $STALE_AFTER seconds
#       (default 7 days — one day below the CLI's TOKEN_REFRESH_INTERVAL
#       of 8d so we refresh before the CLI would mark it stale)
#   then it POSTs the refresh_token to
#   `https://auth.openai.com/oauth/token` (the endpoint baked into the
#   CLI — codex-rs/login/src/auth/manager.rs:94, const REFRESH_TOKEN_URL)
#   with the same CLIENT_ID (`app_EMoamEEZ73f0CkXaXp7hrann`,
#   codex-rs/login/src/auth/manager.rs:928 / v0.130.0:921) and
#   grant_type=refresh_token, and atomically rewrites auth.json in place
#   (write to .tmp + chmod + rename). The CLI tolerates an externally
#   refreshed file: `reload()` is called inside `auth()` whenever the
#   file mtime changes.
#
# Vendor contract source — all field names, endpoint URL, client id,
# response shape verified against `openai/codex@rust-v0.130.0`
# (the exact CLI version pinned in the Dockerfile, line 166):
#   - REFRESH_TOKEN_URL         manager.rs:94
#   - CLIENT_ID                 manager.rs:921
#   - request shape:            { client_id, grant_type:"refresh_token",
#                                 refresh_token }   manager.rs:913-918
#   - response shape:           { id_token?, access_token?,
#                                 refresh_token? } manager.rs:920-925
#   - persist semantics:        update tokens.{id_token,access_token,
#                                 refresh_token} only when present, set
#                                 last_refresh = now (manager.rs:787-810)
#   - stale predicate:          access_token exp <= now OR
#                                 last_refresh < now - 8d
#                                 manager.rs:1786-1808
#
# Constraints honored (RFC §"Constraints honored"):
#   - NEVER echo token values. Filenames + JWT field NAMES + status
#     codes + ISO timestamps only.
#   - No mutation of auth.json outside the atomic-rename path.
#   - Inert when $CODEX_HOME/auth.json is absent OR auth_mode != chatgpt
#     OR refresh_token is empty (the API-key / MiniMax paths don't have
#     refresh tokens; this watchdog is subscription-only by design).
#
# Health sidecar (RFC §"Surface a health metric"):
#   On every loop iteration, write the parsed `last_refresh` ISO and
#   age-in-seconds to $CODEX_HOME/auth_refresh_status.json (mode 0600,
#   agent-owned). The molecule-runtime obs sidecar can scrape this
#   file instead of the auth.json itself — keeps tokens out of the
#   metrics path entirely.

set -uo pipefail

CODEX_HOME="${CODEX_HOME:-/home/agent/.codex}"
AUTH_JSON="${CODEX_HOME}/auth.json"
STATUS_FILE="${CODEX_HOME}/auth_refresh_status.json"

# Resolve python3 portably. PR#19 (internal#569) hardcoded
# /opt/molecule-venv/bin/python3, but the codex image is built FROM
# python:3.11-slim with python3 at /usr/local/bin/python3 — no
# /opt/molecule-venv exists. Every helper invocation exited 127 →
# OAuth refresh never fired → id_token expired silently and Researcher
# wedged upstream of stdout (ae2c3012 diagnosis). Override via
# CODEX_PYTHON for test/dev rigs that need a custom interpreter.
PYTHON_BIN="${CODEX_PYTHON:-$(command -v python3 || true)}"
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
  printf '[codex_auth_refresh %s] FATAL: python3 not found on PATH (CODEX_PYTHON=%s); the watchdog cannot run.\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${CODEX_PYTHON:-<unset>}" >&2
  exit 127
fi

# Tunable via env. Defaults come from RFC §"Proposed fix" + CLI source:
#   - REFRESH_INTERVAL: how often the loop wakes (6h)
#   - SAFETY_MARGIN:    refresh if access_token exp - now < this (4h)
#   - STALE_AFTER:      refresh if last_refresh older than this (7d,
#                       chosen to fire before the CLI's 8d cliff)
REFRESH_INTERVAL="${CODEX_AUTH_REFRESH_INTERVAL_SECONDS:-21600}"
SAFETY_MARGIN="${CODEX_AUTH_SAFETY_MARGIN_SECONDS:-14400}"
STALE_AFTER="${CODEX_AUTH_STALE_AFTER_SECONDS:-604800}"

# Refresh endpoint — keep the CLI's env override honored so a test rig
# can point us at a mock server.
REFRESH_URL="${CODEX_REFRESH_TOKEN_URL_OVERRIDE:-https://auth.openai.com/oauth/token}"
CLIENT_ID="app_EMoamEEZ73f0CkXaXp7hrann"

log() {
  # Stamped, prefixed; never includes token contents.
  printf '[codex_auth_refresh %s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

# Run a one-shot python helper to inspect auth.json and decide whether
# to refresh. Python gives us reliable JSON + JWT decoding without
# pulling in jq (which is not in the image). We deliberately keep the
# helper inline so the script is a single self-contained file.
needs_refresh_and_payload() {
  # Emits one of:
  #   SKIP <reason>
  #   READY <refresh_token> <age_seconds> <iso_last_refresh>
  # Note: <refresh_token> is on stdout for the caller to consume in a
  # variable. Caller MUST NOT echo it. The wrapper below redacts it
  # before any logging.
  "$PYTHON_BIN" - "$AUTH_JSON" "$SAFETY_MARGIN" "$STALE_AFTER" <<'PY'
import base64, json, sys, time
from datetime import datetime, timezone

path = sys.argv[1]
safety_margin = int(sys.argv[2])
stale_after = int(sys.argv[3])

def emit(line):
    print(line, flush=True)

try:
    with open(path, "r", encoding="utf-8") as f:
        blob = json.load(f)
except FileNotFoundError:
    emit("SKIP no_auth_json")
    sys.exit(0)
except (OSError, json.JSONDecodeError) as exc:
    emit(f"SKIP unreadable:{type(exc).__name__}")
    sys.exit(0)

# Subscription-only: API-key / MiniMax paths have no refresh token.
auth_mode = (blob.get("auth_mode") or "").lower()
if auth_mode not in ("chatgpt", "chatgpt_auth_tokens"):
    emit(f"SKIP auth_mode={auth_mode or 'none'}")
    sys.exit(0)

tokens = blob.get("tokens") or {}
refresh_token = tokens.get("refresh_token") or ""
access_token = tokens.get("access_token") or ""
if not refresh_token:
    emit("SKIP no_refresh_token")
    sys.exit(0)

# Parse last_refresh ISO. Tolerate trailing Z (codex emits +00:00 in
# newer versions, Z in 0.130.0 era).
last_refresh_iso = blob.get("last_refresh") or ""
now = datetime.now(timezone.utc)
age_seconds = -1
if last_refresh_iso:
    try:
        iso = last_refresh_iso.replace("Z", "+00:00")
        last_refresh_dt = datetime.fromisoformat(iso)
        if last_refresh_dt.tzinfo is None:
            last_refresh_dt = last_refresh_dt.replace(tzinfo=timezone.utc)
        age_seconds = int((now - last_refresh_dt).total_seconds())
    except ValueError:
        age_seconds = -1

# Decode the access_token JWT `exp` claim (RFC matches the CLI:
# parse_jwt_expiration in codex-rs/login/src/auth/manager.rs).
exp_seconds_remaining = None
if access_token and access_token.count(".") >= 2:
    try:
        payload_b64 = access_token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode()))
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            exp_seconds_remaining = int(exp - time.time())
    except (ValueError, json.JSONDecodeError):
        exp_seconds_remaining = None

needs = False
reasons = []
if exp_seconds_remaining is not None and exp_seconds_remaining <= safety_margin:
    needs = True
    reasons.append(f"exp_in={exp_seconds_remaining}s<={safety_margin}s")
if age_seconds >= 0 and age_seconds >= stale_after:
    needs = True
    reasons.append(f"last_refresh_age={age_seconds}s>={stale_after}s")
if exp_seconds_remaining is None and age_seconds == -1:
    # Defensive: if we can't parse anything, refresh anyway. The CLI's
    # own staleness path would do the same.
    needs = True
    reasons.append("unparseable_metadata")

if not needs:
    emit(
        "SKIP fresh exp_in="
        + (str(exp_seconds_remaining) if exp_seconds_remaining is not None else "unknown")
        + "s age="
        + (str(age_seconds) if age_seconds >= 0 else "unknown")
        + "s"
    )
    sys.exit(0)

# Caller consumes refresh_token from the next line. Reasons echoed
# after for logging.
emit(f"READY {refresh_token}")
print(f"REASONS {','.join(reasons)}", file=sys.stderr)
PY
}

# Apply a refresh response (file path passed as $2) to auth.json
# atomically. We pass the response as a FILE PATH (not stdin) because
# the python helper's body is itself a heredoc on stdin — using stdin
# for the response would collide with the heredoc.
apply_refresh_response() {
  local response_file="$1"
  "$PYTHON_BIN" - "$AUTH_JSON" "$response_file" <<'PY'
import json, os, stat, sys, tempfile
from datetime import datetime, timezone

path = sys.argv[1]
response_path = sys.argv[2]

try:
    with open(path, "r", encoding="utf-8") as f:
        blob = json.load(f)
except (OSError, json.JSONDecodeError) as exc:
    print(f"FAIL load_auth:{type(exc).__name__}", file=sys.stderr)
    sys.exit(2)

try:
    with open(response_path, "r", encoding="utf-8") as f:
        response = json.load(f)
except (OSError, json.JSONDecodeError) as exc:
    print(f"FAIL bad_response_json:{exc.__class__.__name__}", file=sys.stderr)
    sys.exit(2)

if not isinstance(response, dict):
    print("FAIL response_not_object", file=sys.stderr)
    sys.exit(2)

tokens = blob.get("tokens") or {}
# Per manager.rs:787-810: persist only what the response contains.
new_id = response.get("id_token")
new_access = response.get("access_token")
new_refresh = response.get("refresh_token")

if new_id:
    tokens["id_token"] = new_id
if new_access:
    tokens["access_token"] = new_access
if new_refresh:
    tokens["refresh_token"] = new_refresh
blob["tokens"] = tokens
blob["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

# Atomic rename, preserve mode 0600.
dir_path = os.path.dirname(path) or "."
fd, tmp_path = tempfile.mkstemp(prefix=".auth.", suffix=".tmp", dir=dir_path)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(blob, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    os.replace(tmp_path, path)
except Exception:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    raise

# Summary line — token VALUES are not printed, only WHICH fields were
# rotated.
fields = []
if new_id: fields.append("id_token")
if new_access: fields.append("access_token")
if new_refresh: fields.append("refresh_token")
print("OK rotated=" + ",".join(fields) if fields else "OK rotated=<none>")
PY
}

# Write the status sidecar (mode 0600). Never includes token contents.
write_status() {
  local last_iso="$1" age="$2" outcome="$3"
  "$PYTHON_BIN" - "$STATUS_FILE" "$last_iso" "$age" "$outcome" <<'PY'
import json, os, stat, sys
path, last_iso, age, outcome = sys.argv[1:5]
payload = {
    "last_refresh": last_iso,
    "last_refresh_age_seconds": int(age) if age.lstrip("-").isdigit() else None,
    "watchdog_last_outcome": outcome,
    "watchdog_last_run_iso": __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat().replace("+00:00", "Z"),
}
with open(path + ".tmp", "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2)
os.chmod(path + ".tmp", stat.S_IRUSR | stat.S_IWUSR)
os.replace(path + ".tmp", path)
PY
}

# One refresh attempt. Returns 0 on refreshed, 1 on skipped, 2 on
# transient failure (will retry next loop), 3 on permanent failure
# (e.g. refresh_token_expired — operator intervention needed).
attempt_refresh_once() {
  if [ ! -s "$AUTH_JSON" ]; then
    log "skip: $AUTH_JSON absent or empty (no chatgpt-subscription auth in this workspace)"
    write_status "" -1 "skip:no_auth_json"
    return 1
  fi

  # OWNER GATE (codex shared-OAuth durable fix, 2026-05-31): the per-agent
  # OAuth POST is DISABLED BY DEFAULT. The single-use refresh_token must be
  # rotated by exactly ONE owner — the platform-side central refresher
  # (molecule-core internal/codexauth) — because N agents sharing ONE global
  # CODEX_AUTH_JSON each refreshing on their own 401 burns the single-use
  # refresh_token within seconds (each rotation invalidates the siblings).
  # The template NEVER sets CODEX_AUTH_REFRESH_OWNER; workspaces instead GET
  # the current token via codex_auth_sync.sh. CODEX_AUTH_REFRESH_OWNER=1 is
  # reserved for a single operator/owner box that legitimately owns the seed.
  if [ "${CODEX_AUTH_REFRESH_OWNER:-0}" != "1" ]; then
    log "skip: per-agent OAuth refresh is disabled (CODEX_AUTH_REFRESH_OWNER!=1) — token rotation is owned by the platform central refresher; this workspace re-syncs via codex_auth_sync.sh. auth.json untouched."
    write_status "" -1 "skip:not_refresh_owner"
    return 1
  fi

  local decision rc=0
  # Use a temp file so the multi-line decision output is fully captured.
  local decision_file
  decision_file="$(mktemp)"
  needs_refresh_and_payload >"$decision_file" 2>>/tmp/codex_auth_refresh.errlog || rc=$?
  if [ "$rc" -ne 0 ]; then
    log "decision: helper exited $rc; see /tmp/codex_auth_refresh.errlog"
    rm -f "$decision_file"
    write_status "" -1 "skip:helper_error"
    return 2
  fi

  local verdict
  verdict="$(head -n1 "$decision_file")"
  case "$verdict" in
    SKIP*)
      log "no-op: ${verdict#SKIP }"
      rm -f "$decision_file"
      # Re-parse just to refresh the sidecar — separate, doesn't see
      # the token because no refresh body is emitted.
      local last_iso age
      read -r last_iso age <<<"$("$PYTHON_BIN" - "$AUTH_JSON" <<'PY'
import json, sys
from datetime import datetime, timezone
try:
    blob = json.load(open(sys.argv[1]))
except Exception:
    print(' -1'); sys.exit(0)
iso = (blob.get('last_refresh') or '').replace('Z', '+00:00')
age = -1
if iso:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        age = int((datetime.now(timezone.utc) - dt).total_seconds())
    except ValueError:
        pass
print((blob.get('last_refresh') or ''), age)
PY
)"
      write_status "${last_iso:-}" "${age:--1}" "skip"
      return 1
      ;;
    READY*)
      :
      ;;
    *)
      log "decision: unrecognized verdict line (length=${#verdict}); aborting attempt"
      rm -f "$decision_file"
      write_status "" -1 "skip:bad_verdict"
      return 2
      ;;
  esac

  # READY <refresh_token>
  local refresh_token
  refresh_token="${verdict#READY }"
  rm -f "$decision_file"

  if [ -z "$refresh_token" ]; then
    log "decision READY but refresh_token empty; aborting"
    write_status "" -1 "skip:empty_refresh_token"
    return 2
  fi

  log "refresh: starting (endpoint=${REFRESH_URL%%\?*})"

  # Build the request body via python to avoid quoting the token in
  # the shell — token value never appears in argv or env. Send body
  # to curl on stdin via @- ; the shell never sees the token in
  # process arguments.
  local response_file http_code
  response_file="$(mktemp)"
  http_code="$(
    "$PYTHON_BIN" -c "import json,sys; sys.stdout.write(json.dumps({'client_id': '$CLIENT_ID', 'grant_type': 'refresh_token', 'refresh_token': sys.argv[1]}))" "$refresh_token" \
      | curl -sS --max-time 30 \
            -H "Content-Type: application/json" \
            -X POST -d @- \
            -o "$response_file" \
            -w "%{http_code}" \
            "$REFRESH_URL" \
      || echo "000"
  )"
  unset refresh_token

  case "$http_code" in
    2*)
      if apply_refresh_response "$response_file" >>/tmp/codex_auth_refresh.errlog 2>&1; then
        rm -f "$response_file"
        log "refresh: ok (http=$http_code)"
        # Re-read the freshly written file for the sidecar.
        local last_iso age
        read -r last_iso age <<<"$("$PYTHON_BIN" - "$AUTH_JSON" <<'PY'
import json, sys
from datetime import datetime, timezone
blob = json.load(open(sys.argv[1]))
iso = (blob.get('last_refresh') or '').replace('Z', '+00:00')
age = -1
if iso:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    age = int((datetime.now(timezone.utc) - dt).total_seconds())
print((blob.get('last_refresh') or ''), age)
PY
)"
        write_status "${last_iso:-}" "${age:--1}" "refreshed"
        return 0
      else
        log "refresh: apply_refresh_response failed (see /tmp/codex_auth_refresh.errlog)"
        rm -f "$response_file"
        write_status "" -1 "fail:apply_response"
        return 2
      fi
      ;;
    401)
      # Per manager.rs:846-848 — 401 is a PERMANENT failure (refresh
      # token expired / reused / revoked). Operator must re-login on
      # the host running the CTO ChatGPT account.
      log "refresh: PERMANENT FAILURE (http=401) — refresh_token rejected. Operator must re-login the CTO ChatGPT subscription and re-inject CODEX_AUTH_JSON."
      rm -f "$response_file"
      write_status "" -1 "fail:permanent_401"
      return 3
      ;;
    *)
      # Anything else is treated as transient (network glitch, 5xx).
      # Per manager.rs:849-854, the CLI does the same: keep the old
      # tokens and retry later.
      log "refresh: transient failure (http=$http_code); will retry next loop"
      rm -f "$response_file"
      write_status "" -1 "fail:transient_$http_code"
      return 2
      ;;
  esac
}

main_loop() {
  log "watchdog: starting (interval=${REFRESH_INTERVAL}s safety_margin=${SAFETY_MARGIN}s stale_after=${STALE_AFTER}s codex_home=${CODEX_HOME})"
  while true; do
    attempt_refresh_once || true
    sleep "$REFRESH_INTERVAL"
  done
}

# Allow `--once` for manual probe + the boot-time priming probe in
# start.sh. Default is the long-running loop.
case "${1:-}" in
  --once)
    attempt_refresh_once
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
